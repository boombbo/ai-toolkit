import copy
import glob
import os
import time
from collections import OrderedDict

from PIL import Image
from PIL.ImageOps import exif_transpose
from safetensors.torch import save_file, load_file
from torch.utils.data import DataLoader, ConcatDataset
import torch
from torch import nn
from torchvision.transforms import transforms

from jobs.process import BaseTrainProcess
from toolkit.kohya_model_util import load_vae, convert_diffusers_back_to_ldm
from toolkit.data_loader import ImageDataset
from toolkit.losses import ComparativeTotalVariation, get_gradient_penalty
from toolkit.metadata import get_meta_for_safetensors
from toolkit.optimizer import get_optimizer
from toolkit.style import get_style_model_and_losses
from toolkit.train_tools import get_torch_dtype
from diffusers import AutoencoderKL
from tqdm import tqdm
import time
import numpy as np

IMAGE_TRANSFORMS = transforms.Compose(
    [
        transforms.ToTensor(),
        transforms.Normalize([0.5], [0.5]),
    ]
)


def unnormalize(tensor):
    return (tensor / 2 + 0.5).clamp(0, 1)


class Critic:
    process: 'TrainVAEProcess'

    def __init__(
            self,
            learning_rate=1e-5,
            device='cpu',
            optimizer='adam',
            num_critic_per_gen=1,
            dtype='float32',
            lambda_gp=10,
            start_step=0,
            warmup_steps=1000,
            process=None
    ):
        self.learning_rate = learning_rate
        self.device = device
        self.optimizer_type = optimizer
        self.num_critic_per_gen = num_critic_per_gen
        self.dtype = dtype
        self.torch_dtype = get_torch_dtype(self.dtype)
        self.process = process
        self.model = None
        self.optimizer = None
        self.scheduler = None
        self.warmup_steps = warmup_steps
        self.start_step = start_step
        self.lambda_gp = lambda_gp
        self.print = self.process.print
        print(f" Critic config: {self.__dict__}")

    def setup(self):
        from .models.vgg19_critic import Vgg19Critic
        self.model = Vgg19Critic().to(self.device, dtype=self.torch_dtype)
        self.load_weights()
        self.model.train()
        self.model.requires_grad_(True)
        params = self.model.parameters()
        self.optimizer = get_optimizer(params, self.optimizer_type, self.learning_rate)
        self.scheduler = torch.optim.lr_scheduler.ConstantLR(
            self.optimizer,
            total_iters=self.process.max_steps * self.num_critic_per_gen,
            factor=1,
            verbose=False
        )

    def load_weights(self):
        path_to_load = None
        self.print(f"Critic: Looking for latest checkpoint in {self.process.save_root}")
        files = glob.glob(os.path.join(self.process.save_root, f"CRITIC_{self.process.job.name}*.safetensors"))
        if files and len(files) > 0:
            latest_file = max(files, key=os.path.getmtime)
            print(f" - Latest checkpoint is: {latest_file}")
            path_to_load = latest_file
        else:
            self.print(f" - No checkpoint found, starting from scratch")
        if path_to_load:
            self.model.load_state_dict(load_file(path_to_load))

    def save(self, step=None):
        self.process.update_training_metadata()
        save_meta = get_meta_for_safetensors(self.process.meta, self.process.job.name)
        step_num = ''
        if step is not None:
            # zeropad 9 digits
            step_num = f"_{str(step).zfill(9)}"
        save_path = os.path.join(self.process.save_root, f"CRITIC_{self.process.job.name}{step_num}.safetensors")
        save_file(self.model.state_dict(), save_path, save_meta)
        self.print(f"Saved critic to {save_path}")

    def get_critic_loss(self, vgg_output):
        if self.start_step > self.process.step_num:
            return torch.tensor(0.0, dtype=self.torch_dtype, device=self.device)

        warmup_scaler = 1.0
        # we need a warmup when we come on of 1000 steps
        # we want to scale the loss by 0.0 at self.start_step steps and 1.0 at self.start_step + warmup_steps
        if self.process.step_num < self.start_step + self.warmup_steps:
            warmup_scaler = (self.process.step_num - self.start_step) / self.warmup_steps
        # set model to not train for generator loss
        self.model.eval()
        self.model.requires_grad_(False)
        vgg_pred, vgg_target = torch.chunk(vgg_output, 2, dim=0)

        # run model
        stacked_output = self.model(vgg_pred)

        return (-torch.mean(stacked_output)) * warmup_scaler

    def step(self, vgg_output):

        # train critic here
        self.model.train()
        self.model.requires_grad_(True)

        critic_losses = []
        for i in range(self.num_critic_per_gen):
            inputs = vgg_output.detach()
            inputs = inputs.to(self.device, dtype=self.torch_dtype)
            self.optimizer.zero_grad()

            vgg_pred, vgg_target = torch.chunk(inputs, 2, dim=0)

            stacked_output = self.model(inputs)
            out_pred, out_target = torch.chunk(stacked_output, 2, dim=0)

            # Compute gradient penalty
            gradient_penalty = get_gradient_penalty(self.model, vgg_target, vgg_pred, self.device)

            # Compute WGAN-GP critic loss
            critic_loss = -(torch.mean(out_target) - torch.mean(out_pred)) + self.lambda_gp * gradient_penalty
            critic_loss.backward()
            self.optimizer.zero_grad()
            self.optimizer.step()
            self.scheduler.step()
            critic_losses.append(critic_loss.item())

        # avg loss
        loss = np.mean(critic_losses)
        return loss

    def get_lr(self):
        if self.optimizer_type.startswith('dadaptation'):
            learning_rate = (
                    self.optimizer.param_groups[0]["d"] *
                    self.optimizer.param_groups[0]["lr"]
            )
        else:
            learning_rate = self.optimizer.param_groups[0]['lr']

        return learning_rate


class TrainVAEProcess(BaseTrainProcess):
    def __init__(self, process_id: int, job, config: OrderedDict):
        super().__init__(process_id, job, config)
        self.data_loader = None
        self.vae = None
        self.device = self.get_conf('device', self.job.device)
        self.vae_path = self.get_conf('vae_path', required=True)
        self.datasets_objects = self.get_conf('datasets', required=True)
        self.training_folder = self.get_conf('training_folder', self.job.training_folder)
        self.batch_size = self.get_conf('batch_size', 1, as_type=int)
        self.resolution = self.get_conf('resolution', 256, as_type=int)
        self.learning_rate = self.get_conf('learning_rate', 1e-6, as_type=float)
        self.sample_every = self.get_conf('sample_every', None)
        self.optimizer_type = self.get_conf('optimizer', 'adam')
        self.epochs = self.get_conf('epochs', None, as_type=int)
        self.max_steps = self.get_conf('max_steps', None, as_type=int)
        self.save_every = self.get_conf('save_every', None)
        self.dtype = self.get_conf('dtype', 'float32')
        self.sample_sources = self.get_conf('sample_sources', None)
        self.log_every = self.get_conf('log_every', 100, as_type=int)
        self.style_weight = self.get_conf('style_weight', 0, as_type=float)
        self.content_weight = self.get_conf('content_weight', 0, as_type=float)
        self.kld_weight = self.get_conf('kld_weight', 0, as_type=float)
        self.mse_weight = self.get_conf('mse_weight', 1e0, as_type=float)
        self.tv_weight = self.get_conf('tv_weight', 1e0, as_type=float)
        self.critic_weight = self.get_conf('critic_weight', 1, as_type=float)

        self.blocks_to_train = self.get_conf('blocks_to_train', ['all'])
        self.writer = self.job.writer
        self.torch_dtype = get_torch_dtype(self.dtype)
        self.save_root = os.path.join(self.training_folder, self.job.name)
        self.vgg_19 = None
        self.progress_bar = None
        self.style_weight_scalers = []
        self.content_weight_scalers = []

        self.step_num = 0
        self.epoch_num = 0

        self.use_critic = self.get_conf('use_critic', False, as_type=bool)
        self.critic = None

        if self.use_critic:
            self.critic = Critic(
                device=self.device,
                dtype=self.dtype,
                process=self,
                **self.get_conf('critic', {})  # pass any other params
            )

        if self.sample_every is not None and self.sample_sources is None:
            raise ValueError('sample_every is specified but sample_sources is not')

        if self.epochs is None and self.max_steps is None:
            raise ValueError('epochs or max_steps must be specified')

        self.data_loaders = []
        # check datasets
        assert isinstance(self.datasets_objects, list)
        for dataset in self.datasets_objects:
            if 'path' not in dataset:
                raise ValueError('dataset must have a path')
            # check if is dir
            if not os.path.isdir(dataset['path']):
                raise ValueError(f"dataset path does is not a directory: {dataset['path']}")

        # make training folder
        if not os.path.exists(self.save_root):
            os.makedirs(self.save_root, exist_ok=True)

    def update_training_metadata(self):
        self.add_meta(OrderedDict({"training_info": self.get_training_info()}))

    def get_training_info(self):
        info = OrderedDict({
            'step': self.step_num,
            'epoch': self.epoch_num,
        })
        return info

    def print(self, message, **kwargs):
        if self.progress_bar is not None:
            self.progress_bar.write(message, **kwargs)
            self.progress_bar.update()
        else:
            print(message, **kwargs)

    def load_datasets(self):
        if self.data_loader is None:
            print(f"Loading datasets")
            datasets = []
            for dataset in self.datasets_objects:
                print(f" - Dataset: {dataset['path']}")
                ds = copy.copy(dataset)
                ds['resolution'] = self.resolution
                image_dataset = ImageDataset(ds)
                datasets.append(image_dataset)

            concatenated_dataset = ConcatDataset(datasets)
            self.data_loader = DataLoader(
                concatenated_dataset,
                batch_size=self.batch_size,
                shuffle=True,
                num_workers=6
            )

    def setup_vgg19(self):
        if self.vgg_19 is None:
            self.vgg_19, self.style_losses, self.content_losses, self.vgg19_pool_4 = get_style_model_and_losses(
                single_target=True,
                device=self.device,
                output_layer_name='pool_4',
                dtype=self.torch_dtype
            )
            self.vgg_19.to(self.device, dtype=self.torch_dtype)
            self.vgg_19.requires_grad_(False)

            # we run random noise through first to get layer scalers to normalize the loss per layer
            # bs of 2 because we run pred and target through stacked
            noise = torch.randn((2, 3, self.resolution, self.resolution), device=self.device, dtype=self.torch_dtype)
            self.vgg_19(noise)
            for style_loss in self.style_losses:
                # get a scaler  to normalize to 1
                scaler = 1 / torch.mean(style_loss.loss).item()
                self.style_weight_scalers.append(scaler)
            for content_loss in self.content_losses:
                # get a scaler  to normalize to 1
                scaler = 1 / torch.mean(content_loss.loss).item()
                self.content_weight_scalers.append(scaler)

            self.print(f"Style weight scalers: {self.style_weight_scalers}")
            self.print(f"Content weight scalers: {self.content_weight_scalers}")

    def get_style_loss(self):
        if self.style_weight > 0:
            # scale all losses with loss scalers
            loss = torch.sum(
                torch.stack([loss.loss * scaler for loss, scaler in zip(self.style_losses, self.style_weight_scalers)]))
            return loss
        else:
            return torch.tensor(0.0, device=self.device)

    def get_content_loss(self):
        if self.content_weight > 0:
            # scale all losses with loss scalers
            loss = torch.sum(torch.stack(
                [loss.loss * scaler for loss, scaler in zip(self.content_losses, self.content_weight_scalers)]))
            return loss
        else:
            return torch.tensor(0.0, device=self.device)

    def get_mse_loss(self, pred, target):
        if self.mse_weight > 0:
            loss_fn = nn.MSELoss()
            loss = loss_fn(pred, target)
            return loss
        else:
            return torch.tensor(0.0, device=self.device)

    def get_kld_loss(self, mu, log_var):
        if self.kld_weight > 0:
            # Kullback-Leibler divergence
            # added here for full training (not implemented). Not needed for only decoder
            # as we are not changing the distribution of the latent space
            # normally it would help keep a normal distribution for latents
            KLD = -0.5 * torch.sum(1 + log_var - mu.pow(2) - log_var.exp())  # KL divergence
            return KLD
        else:
            return torch.tensor(0.0, device=self.device)

    def get_tv_loss(self, pred, target):
        if self.tv_weight > 0:
            get_tv_loss = ComparativeTotalVariation()
            loss = get_tv_loss(pred, target)
            return loss
        else:
            return torch.tensor(0.0, device=self.device)

    def save(self, step=None):
        if not os.path.exists(self.save_root):
            os.makedirs(self.save_root, exist_ok=True)

        step_num = ''
        if step is not None:
            # zeropad 9 digits
            step_num = f"_{str(step).zfill(9)}"

        self.update_training_metadata()
        filename = f'{self.job.name}{step_num}.safetensors'
        # prepare meta
        save_meta = get_meta_for_safetensors(self.meta, self.job.name)

        state_dict = convert_diffusers_back_to_ldm(self.vae)

        for key in list(state_dict.keys()):
            v = state_dict[key]
            v = v.detach().clone().to("cpu").to(torch.float32)
            state_dict[key] = v

        # having issues with meta
        save_file(state_dict, os.path.join(self.save_root, filename), save_meta)

        self.print(f"Saved to {os.path.join(self.save_root, filename)}")

        if self.use_critic:
            self.critic.save(step)

    def sample(self, step=None):
        sample_folder = os.path.join(self.save_root, 'samples')
        if not os.path.exists(sample_folder):
            os.makedirs(sample_folder, exist_ok=True)

        with torch.no_grad():
            for i, img_url in enumerate(self.sample_sources):
                img = exif_transpose(Image.open(img_url))
                img = img.convert('RGB')
                # crop if not square
                if img.width != img.height:
                    min_dim = min(img.width, img.height)
                    img = img.crop((0, 0, min_dim, min_dim))
                # resize
                img = img.resize((self.resolution, self.resolution))

                input_img = img
                img = IMAGE_TRANSFORMS(img).unsqueeze(0).to(self.device, dtype=self.torch_dtype)
                img = img
                decoded = self.vae(img).sample
                decoded = (decoded / 2 + 0.5).clamp(0, 1)
                # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
                decoded = decoded.cpu().permute(0, 2, 3, 1).squeeze(0).float().numpy()

                # convert to pillow image
                decoded = Image.fromarray((decoded * 255).astype(np.uint8))

                # stack input image and decoded image
                input_img = input_img.resize((self.resolution, self.resolution))
                decoded = decoded.resize((self.resolution, self.resolution))

                output_img = Image.new('RGB', (self.resolution * 2, self.resolution))
                output_img.paste(input_img, (0, 0))
                output_img.paste(decoded, (self.resolution, 0))

                step_num = ''
                if step is not None:
                    # zero-pad 9 digits
                    step_num = f"_{str(step).zfill(9)}"
                seconds_since_epoch = int(time.time())
                # zero-pad 2 digits
                i_str = str(i).zfill(2)
                filename = f"{seconds_since_epoch}{step_num}_{i_str}.png"
                output_img.save(os.path.join(sample_folder, filename))

    def load_vae(self):
        path_to_load = self.vae_path
        # see if we have a checkpoint in out output to resume from
        self.print(f"Looking for latest checkpoint in {self.save_root}")
        files = glob.glob(os.path.join(self.save_root, f"{self.job.name}*.safetensors"))
        if files and len(files) > 0:
            latest_file = max(files, key=os.path.getmtime)
            print(f" - Latest checkpoint is: {latest_file}")
            path_to_load = latest_file
            # todo update step and epoch count
        else:
            self.print(f" - No checkpoint found, starting from scratch")
        # load vae
        self.print(f"Loading VAE")
        self.print(f" - Loading VAE: {path_to_load}")
        if self.vae is None:
            self.vae = load_vae(path_to_load, dtype=self.torch_dtype)

        # set decoder to train
        self.vae.to(self.device, dtype=self.torch_dtype)
        self.vae.requires_grad_(False)
        self.vae.eval()
        self.vae.decoder.train()

    def run(self):
        super().run()
        self.load_datasets()

        max_step_epochs = self.max_steps // len(self.data_loader)
        num_epochs = self.epochs
        if num_epochs is None or num_epochs > max_step_epochs:
            num_epochs = max_step_epochs

        max_epoch_steps = len(self.data_loader) * num_epochs
        num_steps = self.max_steps
        if num_steps is None or num_steps > max_epoch_steps:
            num_steps = max_epoch_steps
        self.max_steps = num_steps
        self.epochs = num_epochs
        start_step = self.step_num

        self.print(f"Training VAE")
        self.print(f" - Training folder: {self.training_folder}")
        self.print(f" - Batch size: {self.batch_size}")
        self.print(f" - Learning rate: {self.learning_rate}")
        self.print(f" - Epochs: {num_epochs}")
        self.print(f" - Max steps: {self.max_steps}")

        # load vae
        self.load_vae()

        params = []

        # only set last 2 layers to trainable
        for param in self.vae.decoder.parameters():
            param.requires_grad = False

        train_all = 'all' in self.blocks_to_train

        if train_all:
            params = list(self.vae.decoder.parameters())
            self.vae.decoder.requires_grad_(True)
        else:
            # mid_block
            if train_all or 'mid_block' in self.blocks_to_train:
                params += list(self.vae.decoder.mid_block.parameters())
                self.vae.decoder.mid_block.requires_grad_(True)
            # up_blocks
            if train_all or 'up_blocks' in self.blocks_to_train:
                params += list(self.vae.decoder.up_blocks.parameters())
                self.vae.decoder.up_blocks.requires_grad_(True)
            # conv_out (single conv layer output)
            if train_all or 'conv_out' in self.blocks_to_train:
                params += list(self.vae.decoder.conv_out.parameters())
                self.vae.decoder.conv_out.requires_grad_(True)

        if self.style_weight > 0 or self.content_weight > 0 or self.use_critic:
            self.setup_vgg19()
            self.vgg_19.requires_grad_(False)
            self.vgg_19.eval()
            if self.use_critic:
                self.critic.setup()

        optimizer = get_optimizer(params, self.optimizer_type, self.learning_rate)

        # setup scheduler
        # todo allow other schedulers
        scheduler = torch.optim.lr_scheduler.ConstantLR(
            optimizer,
            total_iters=num_steps,
            factor=1,
            verbose=False
        )

        # setup tqdm progress bar
        self.progress_bar = tqdm(
            total=num_steps,
            desc='Training VAE',
            leave=True
        )

        # sample first
        self.sample()
        blank_losses = OrderedDict({
            "total": [],
            "style": [],
            "content": [],
            "mse": [],
            "kl": [],
            "tv": [],
            "crD": [],
            "crG": [],
        })
        epoch_losses = copy.deepcopy(blank_losses)
        log_losses = copy.deepcopy(blank_losses)
        # range start at self.epoch_num go to self.epochs
        for epoch in range(self.epoch_num, self.epochs, 1):
            if self.step_num >= self.max_steps:
                break
            for batch in self.data_loader:
                if self.step_num >= self.max_steps:
                    break

                batch = batch.to(self.device, dtype=self.torch_dtype)

                # forward pass
                dgd = self.vae.encode(batch).latent_dist
                mu, logvar = dgd.mean, dgd.logvar
                latents = dgd.sample()
                latents.requires_grad_(True)

                pred = self.vae.decode(latents).sample

                # Run through VGG19
                if self.style_weight > 0 or self.content_weight > 0 or self.use_critic:
                    stacked = torch.cat([pred, batch], dim=0)
                    stacked = (stacked / 2 + 0.5).clamp(0, 1)
                    self.vgg_19(stacked)

                if self.use_critic:
                    critic_d_loss = self.critic.step(self.vgg19_pool_4.tensor.detach())
                else:
                    critic_d_loss = 0.0

                style_loss = self.get_style_loss() * self.style_weight
                content_loss = self.get_content_loss() * self.content_weight
                kld_loss = self.get_kld_loss(mu, logvar) * self.kld_weight
                mse_loss = self.get_mse_loss(pred, batch) * self.mse_weight
                tv_loss = self.get_tv_loss(pred, batch) * self.tv_weight
                if self.use_critic:
                    critic_gen_loss = self.critic.get_critic_loss(self.vgg19_pool_4.tensor) * self.critic_weight
                else:
                    critic_gen_loss = torch.tensor(0.0, device=self.device, dtype=self.torch_dtype)

                loss = style_loss + content_loss + kld_loss + mse_loss + tv_loss + critic_gen_loss

                # Backward pass and optimization
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                scheduler.step()

                # update progress bar
                loss_value = loss.item()
                # get exponent like 3.54e-4
                loss_string = f"loss: {loss_value:.2e}"
                if self.content_weight > 0:
                    loss_string += f" cnt: {content_loss.item():.2e}"
                if self.style_weight > 0:
                    loss_string += f" sty: {style_loss.item():.2e}"
                if self.kld_weight > 0:
                    loss_string += f" kld: {kld_loss.item():.2e}"
                if self.mse_weight > 0:
                    loss_string += f" mse: {mse_loss.item():.2e}"
                if self.tv_weight > 0:
                    loss_string += f" tv: {tv_loss.item():.2e}"
                if self.use_critic and self.critic_weight > 0:
                    loss_string += f" crG: {critic_gen_loss.item():.2e}"
                if self.use_critic:
                    loss_string += f" crD: {critic_d_loss:.2e}"

                if self.optimizer_type.startswith('dadaptation'):
                    learning_rate = (
                            optimizer.param_groups[0]["d"] *
                            optimizer.param_groups[0]["lr"]
                    )
                else:
                    learning_rate = optimizer.param_groups[0]['lr']

                lr_critic_string = ''
                if self.use_critic:
                    lr_critic = self.critic.get_lr()
                    lr_critic_string = f" lrC: {lr_critic:.1e}"

                self.progress_bar.set_postfix_str(f"lr: {learning_rate:.1e}{lr_critic_string} {loss_string}")
                self.progress_bar.set_description(f"E: {epoch}")
                self.progress_bar.update(1)

                epoch_losses["total"].append(loss_value)
                epoch_losses["style"].append(style_loss.item())
                epoch_losses["content"].append(content_loss.item())
                epoch_losses["mse"].append(mse_loss.item())
                epoch_losses["kl"].append(kld_loss.item())
                epoch_losses["tv"].append(tv_loss.item())
                epoch_losses["crG"].append(critic_gen_loss.item())
                epoch_losses["crD"].append(critic_d_loss)

                log_losses["total"].append(loss_value)
                log_losses["style"].append(style_loss.item())
                log_losses["content"].append(content_loss.item())
                log_losses["mse"].append(mse_loss.item())
                log_losses["kl"].append(kld_loss.item())
                log_losses["tv"].append(tv_loss.item())
                log_losses["crG"].append(critic_gen_loss.item())
                log_losses["crD"].append(critic_d_loss)

                # don't do on first step
                if self.step_num != start_step:
                    if self.sample_every and self.step_num % self.sample_every == 0:
                        # print above the progress bar
                        self.print(f"Sampling at step {self.step_num}")
                        self.sample(self.step_num)

                    if self.save_every and self.step_num % self.save_every == 0:
                        # print above the progress bar
                        self.print(f"Saving at step {self.step_num}")
                        self.save(self.step_num)

                    if self.log_every and self.step_num % self.log_every == 0:
                        # log to tensorboard
                        if self.writer is not None:
                            # get avg loss
                            for key in log_losses:
                                log_losses[key] = sum(log_losses[key]) / len(log_losses[key])
                                # if log_losses[key] > 0:
                                self.writer.add_scalar(f"loss/{key}", log_losses[key], self.step_num)
                        # reset log losses
                        log_losses = copy.deepcopy(blank_losses)

                self.step_num += 1
            # end epoch
            if self.writer is not None:
                # get avg loss
                for key in epoch_losses:
                    epoch_losses[key] = sum(log_losses[key]) / len(log_losses[key])
                    if epoch_losses[key] > 0:
                        self.writer.add_scalar(f"epoch loss/{key}", epoch_losses[key], epoch)
            # reset epoch losses
            epoch_losses = copy.deepcopy(blank_losses)

        self.save()
