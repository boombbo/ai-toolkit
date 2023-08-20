import glob
from collections import OrderedDict
import os
from typing import Union

from torch.utils.data import DataLoader

from toolkit.lora_special import LoRASpecialNetwork
from toolkit.optimizer import get_optimizer

from toolkit.scheduler import get_lr_scheduler
from toolkit.stable_diffusion_model import StableDiffusion

from jobs.process import BaseTrainProcess
from toolkit.metadata import get_meta_for_safetensors, load_metadata_from_safetensors, add_base_model_info_to_meta
from toolkit.train_tools import get_torch_dtype
import gc

import torch
from tqdm import tqdm

from toolkit.config_modules import SaveConfig, LogingConfig, SampleConfig, NetworkConfig, TrainConfig, ModelConfig, \
    GenerateImageConfig


def flush():
    torch.cuda.empty_cache()
    gc.collect()


class BaseSDTrainProcess(BaseTrainProcess):
    sd: StableDiffusion

    def __init__(self, process_id: int, job, config: OrderedDict, custom_pipeline=None):
        super().__init__(process_id, job, config)
        self.custom_pipeline = custom_pipeline
        self.step_num = 0
        self.start_step = 0
        self.device = self.get_conf('device', self.job.device)
        self.device_torch = torch.device(self.device)
        network_config = self.get_conf('network', None)
        if network_config is not None:
            self.network_config = NetworkConfig(**network_config)
        else:
            self.network_config = None
        self.train_config = TrainConfig(**self.get_conf('train', {}))
        self.model_config = ModelConfig(**self.get_conf('model', {}))
        self.save_config = SaveConfig(**self.get_conf('save', {}))
        self.sample_config = SampleConfig(**self.get_conf('sample', {}))
        first_sample_config = self.get_conf('first_sample', None)
        if first_sample_config is not None:
            self.has_first_sample_requested = True
            self.first_sample_config = SampleConfig(**first_sample_config)
        else:
            self.has_first_sample_requested = False
            self.first_sample_config = self.sample_config
        self.logging_config = LogingConfig(**self.get_conf('logging', {}))
        self.optimizer = None
        self.lr_scheduler = None
        self.data_loader: Union[DataLoader, None] = None

        self.sd = StableDiffusion(
            device=self.device,
            model_config=self.model_config,
            dtype=self.train_config.dtype,
            custom_pipeline=self.custom_pipeline,
        )

        # to hold network if there is one
        self.network = None

    def sample(self, step=None, is_first=False):
        sample_folder = os.path.join(self.save_root, 'samples')
        gen_img_config_list = []

        sample_config = self.first_sample_config if is_first else self.sample_config
        start_seed = sample_config.seed
        current_seed = start_seed
        for i in range(len(sample_config.prompts)):
            if sample_config.walk_seed:
                current_seed = start_seed + i

            step_num = ''
            if step is not None:
                # zero-pad 9 digits
                step_num = f"_{str(step).zfill(9)}"

            filename = f"[time]_{step_num}_[count].png"

            output_path = os.path.join(sample_folder, filename)

            gen_img_config_list.append(GenerateImageConfig(
                prompt=sample_config.prompts[i],  # it will autoparse the prompt
                width=sample_config.width,
                height=sample_config.height,
                negative_prompt=sample_config.neg,
                seed=current_seed,
                guidance_scale=sample_config.guidance_scale,
                guidance_rescale=sample_config.guidance_rescale,
                num_inference_steps=sample_config.sample_steps,
                network_multiplier=sample_config.network_multiplier,
                output_path=output_path,
            ))

        # send to be generated
        self.sd.generate_images(gen_img_config_list)

    def update_training_metadata(self):
        o_dict = OrderedDict({
            "training_info": self.get_training_info()
        })
        if self.model_config.is_v2:
            o_dict['ss_v2'] = True
            o_dict['ss_base_model_version'] = 'sd_2.1'

        elif self.model_config.is_xl:
            o_dict['ss_base_model_version'] = 'sdxl_1.0'
        else:
            o_dict['ss_base_model_version'] = 'sd_1.5'

        o_dict = add_base_model_info_to_meta(
            o_dict,
            is_v2=self.model_config.is_v2,
            is_xl=self.model_config.is_xl,
        )
        o_dict['ss_output_name'] = self.job.name

        self.add_meta(o_dict)

    def get_training_info(self):
        info = OrderedDict({
            'step': self.step_num + 1
        })
        return info

    def clean_up_saves(self):
        # remove old saves
        # get latest saved step
        if os.path.exists(self.save_root):
            latest_file = None
            # pattern is {job_name}_{zero_filles_step}.safetensors but NOT {job_name}.safetensors
            pattern = f"{self.job.name}_*.safetensors"
            files = glob.glob(os.path.join(self.save_root, pattern))
            if len(files) > self.save_config.max_step_saves_to_keep:
                # remove all but the latest max_step_saves_to_keep
                files.sort(key=os.path.getctime)
                for file in files[:-self.save_config.max_step_saves_to_keep]:
                    self.print(f"Removing old save: {file}")
                    os.remove(file)
            return latest_file
        else:
            return None

    def save(self, step=None):
        if not os.path.exists(self.save_root):
            os.makedirs(self.save_root, exist_ok=True)

        step_num = ''
        if step is not None:
            # zeropad 9 digits
            step_num = f"_{str(step).zfill(9)}"

        self.update_training_metadata()
        filename = f'{self.job.name}{step_num}.safetensors'
        file_path = os.path.join(self.save_root, filename)
        # prepare meta
        save_meta = get_meta_for_safetensors(self.meta, self.job.name)
        if self.network is not None:
            prev_multiplier = self.network.multiplier
            self.network.multiplier = 1.0
            # TODO handle dreambooth, fine tuning, etc
            self.network.save_weights(
                file_path,
                dtype=get_torch_dtype(self.save_config.dtype),
                metadata=save_meta
            )
            self.network.multiplier = prev_multiplier
        else:
            self.sd.save(
                file_path,
                save_meta,
                get_torch_dtype(self.save_config.dtype)
            )

        self.print(f"Saved to {file_path}")
        self.clean_up_saves()

    # Called before the model is loaded
    def hook_before_model_load(self):
        # override in subclass
        pass

    def hook_add_extra_train_params(self, params):
        # override in subclass
        return params

    def hook_before_train_loop(self):
        pass

    def hook_train_loop(self, batch=None):
        # return loss
        return 0.0

    def get_latest_save_path(self):
        # get latest saved step
        if os.path.exists(self.save_root):
            latest_file = None
            # pattern is {job_name}_{zero_filles_step}.safetensors or {job_name}.safetensors
            pattern = f"{self.job.name}*.safetensors"
            files = glob.glob(os.path.join(self.save_root, pattern))
            if len(files) > 0:
                latest_file = max(files, key=os.path.getctime)
            return latest_file
        else:
            return None

    def load_weights(self, path):
        if self.network is not None:
            self.network.load_weights(path)
            meta = load_metadata_from_safetensors(path)
            # if 'training_info' in Orderdict keys
            if 'training_info' in meta and 'step' in meta['training_info']:
                self.step_num = meta['training_info']['step']
                self.start_step = self.step_num
                print(f"Found step {self.step_num} in metadata, starting from there")

        else:
            print("load_weights not implemented for non-network models")

    def run(self):
        # run base process run
        BaseTrainProcess.run(self)
        ### HOOK ###
        self.hook_before_model_load()
        # run base sd process run
        self.sd.load_model()

        dtype = get_torch_dtype(self.train_config.dtype)

        # model is loaded from BaseSDProcess
        unet = self.sd.unet
        vae = self.sd.vae
        tokenizer = self.sd.tokenizer
        text_encoder = self.sd.text_encoder
        noise_scheduler = self.sd.noise_scheduler

        if self.train_config.xformers:
            vae.set_use_memory_efficient_attention_xformers(True)
            unet.enable_xformers_memory_efficient_attention()
        if self.train_config.gradient_checkpointing:
            unet.enable_gradient_checkpointing()
            # if isinstance(text_encoder, list):
            #     for te in text_encoder:
            #         te.enable_gradient_checkpointing()
            # else:
            #     text_encoder.enable_gradient_checkpointing()

        unet.to(self.device_torch, dtype=dtype)
        unet.requires_grad_(False)
        unet.eval()
        vae = vae.to(torch.device('cpu'), dtype=dtype)
        vae.requires_grad_(False)
        vae.eval()

        if self.network_config is not None:
            self.network = LoRASpecialNetwork(
                text_encoder=text_encoder,
                unet=unet,
                lora_dim=self.network_config.linear,
                multiplier=1.0,
                alpha=self.network_config.linear_alpha,
                train_unet=self.train_config.train_unet,
                train_text_encoder=self.train_config.train_text_encoder,
                conv_lora_dim=self.network_config.conv,
                conv_alpha=self.network_config.conv_alpha,
            )

            self.network.force_to(self.device_torch, dtype=dtype)
            # give network to sd so it can use it
            self.sd.network = self.network

            self.network.apply_to(
                text_encoder,
                unet,
                self.train_config.train_text_encoder,
                self.train_config.train_unet
            )

            self.network.prepare_grad_etc(text_encoder, unet)

            params = self.network.prepare_optimizer_params(
                text_encoder_lr=self.train_config.lr,
                unet_lr=self.train_config.lr,
                default_lr=self.train_config.lr
            )

            if self.train_config.gradient_checkpointing:
                self.network.enable_gradient_checkpointing()

            latest_save_path = self.get_latest_save_path()
            if latest_save_path is not None:
                self.print(f"#### IMPORTANT RESUMING FROM {latest_save_path} ####")
                self.print(f"Loading from {latest_save_path}")
                self.load_weights(latest_save_path)
                self.network.multiplier = 1.0


        else:
            params = []
            # assume dreambooth/finetune
            if self.train_config.train_text_encoder:
                if self.sd.is_xl:
                    for te in text_encoder:
                        te.requires_grad_(True)
                        te.train()
                        params += te.parameters()
                else:
                    text_encoder.requires_grad_(True)
                    text_encoder.train()
                    params += text_encoder.parameters()
            if self.train_config.train_unet:
                unet.requires_grad_(True)
                unet.train()
                params += unet.parameters()

        ### HOOK ###
        params = self.hook_add_extra_train_params(params)

        optimizer_type = self.train_config.optimizer.lower()
        optimizer = get_optimizer(params, optimizer_type, learning_rate=self.train_config.lr,
                                  optimizer_params=self.train_config.optimizer_params)
        self.optimizer = optimizer

        lr_scheduler = get_lr_scheduler(
            self.train_config.lr_scheduler,
            optimizer,
            max_iterations=self.train_config.steps,
            lr_min=self.train_config.lr / 100,
        )

        self.lr_scheduler = lr_scheduler

        ### HOOK ###
        self.hook_before_train_loop()

        if self.has_first_sample_requested:
            self.print("Generating first sample from first sample config")
            self.sample(0, is_first=True)

        # sample first
        if self.train_config.skip_first_sample:
            self.print("Skipping first sample due to config setting")
        else:
            self.print("Generating baseline samples before training")
            self.sample(0)

        self.progress_bar = tqdm(
            total=self.train_config.steps,
            desc=self.job.name,
            leave=True,
            initial=self.step_num,
            iterable=range(0, self.train_config.steps),
        )

        if self.data_loader is not None:
            dataloader = self.data_loader
            dataloader_iterator = iter(dataloader)
        else:
            dataloader = None
            dataloader_iterator = None

        # self.step_num = 0
        for step in range(self.step_num, self.train_config.steps):
            if dataloader is not None:
                try:
                    batch = next(dataloader_iterator)
                except StopIteration:
                    # hit the end of an epoch, reset
                    # todo, should we do something else here? like blow up balloons?
                    dataloader_iterator = iter(dataloader)
                    batch = next(dataloader_iterator)
            else:
                batch = None

            ### HOOK ###
            loss_dict = self.hook_train_loop(batch)
            flush()

            if self.train_config.optimizer.lower().startswith('dadaptation') or \
                    self.train_config.optimizer.lower().startswith('prodigy'):
                learning_rate = (
                        optimizer.param_groups[0]["d"] *
                        optimizer.param_groups[0]["lr"]
                )
            else:
                learning_rate = optimizer.param_groups[0]['lr']

            prog_bar_string = f"lr: {learning_rate:.1e}"
            for key, value in loss_dict.items():
                prog_bar_string += f" {key}: {value:.3e}"

            self.progress_bar.set_postfix_str(prog_bar_string)

            # don't do on first step
            if self.step_num != self.start_step:
                # pause progress bar
                self.progress_bar.unpause()  # makes it so doesn't track time
                if self.sample_config.sample_every and self.step_num % self.sample_config.sample_every == 0:
                    # print above the progress bar
                    self.sample(self.step_num)

                if self.save_config.save_every and self.step_num % self.save_config.save_every == 0:
                    # print above the progress bar
                    self.print(f"Saving at step {self.step_num}")
                    self.save(self.step_num)

                if self.logging_config.log_every and self.step_num % self.logging_config.log_every == 0:
                    # log to tensorboard
                    if self.writer is not None:
                        for key, value in loss_dict.items():
                            self.writer.add_scalar(f"{key}", value, self.step_num)
                        self.writer.add_scalar(f"lr", learning_rate, self.step_num)
                self.progress_bar.refresh()

            # sets progress bar to match out step
            self.progress_bar.update(step - self.progress_bar.n)
            # end of step
            self.step_num = step

        self.sample(self.step_num + 1)
        print("")
        self.save()

        del (
            self.sd,
            unet,
            noise_scheduler,
            optimizer,
            self.network,
            tokenizer,
            text_encoder,
        )

        flush()