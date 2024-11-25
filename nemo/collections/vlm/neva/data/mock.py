# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Dict, List, Optional

import lightning.pytorch as pl
import numpy as np
import torch
from lightning.pytorch.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS
from torch.utils import data
from torch.utils.data import DataLoader, Dataset

from nemo.collections.vlm.neva.data.multimodal_tokens import IMAGE_TOKEN_INDEX
from nemo.lightning.pytorch.plugins import MegatronDataSampler
from nemo.utils import logging


class MockDataModule(pl.LightningDataModule):
    def __init__(
        self,
        seq_length: int = 2048,
        decoder_seq_length: Optional[int] = None,
        tokenizer: Optional = None,
        image_processor: Optional = None,
        micro_batch_size: int = 4,
        global_batch_size: int = 8,
        rampup_batch_size: Optional[List[int]] = None,
        num_train_samples: int = 10_000_000,
        num_val_samples: int = 10_000_000,
        num_test_samples: int = 10_000_000,
        num_workers: int = 8,
        pin_memory: bool = True,
        persistent_workers: bool = False,
        is_llava_next=False,
    ):
        super().__init__()
        self.seq_length = seq_length
        self.decoder_seq_len = decoder_seq_length
        self.num_train_samples = num_train_samples
        self.num_val_samples = num_val_samples
        self.num_test_samples = num_test_samples
        self.num_workers = num_workers
        self.pin_memory = pin_memory
        self.persistent_workers = persistent_workers
        self.is_llava_next = is_llava_next

        if tokenizer is None or image_processor is None:
            logging.warning(f"Processor or tokenizer are not provided! Fall back to `llava-hf/llava-1.5-7b-hf`.")
            from transformers import AutoProcessor
            from nemo.collections.common.tokenizers.huggingface.auto_tokenizer import AutoTokenizer

            if is_llava_next:
                model_name = "llava-hf/llava-v1.6-vicuna-7b-hf"
            else:
                model_name = "llava-hf/llava-1.5-7b-hf"

            processor = AutoProcessor.from_pretrained(model_name)
            self.tokenizer = tokenizer or AutoTokenizer(model_name)
            self.image_processor = image_processor or processor.image_processor
        self.data_sampler = MegatronDataSampler(
            seq_len=self.seq_length,
            decoder_seq_len=self.decoder_seq_len,
            micro_batch_size=micro_batch_size,
            global_batch_size=global_batch_size,
            rampup_batch_size=rampup_batch_size,
        )

    def setup(self, stage: str = "") -> None:
        self._train_ds = _MockNevaDataset(
            self.tokenizer, self.image_processor, self.is_llava_next, "train", self.num_train_samples, self.seq_length
        )
        self._validation_ds = _MockNevaDataset(
            self.tokenizer, self.image_processor, self.is_llava_next, "valid", self.num_val_samples, self.seq_length
        )
        self._test_ds = _MockNevaDataset(
            self.tokenizer, self.image_processor, self.is_llava_next, "test", self.num_test_samples, self.seq_length
        )

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        if not hasattr(self, "_train_ds"):
            self.setup()
        return self._create_dataloader(self._train_ds)

    def val_dataloader(self) -> EVAL_DATALOADERS:
        if not hasattr(self, "_validation_ds"):
            self.setup()
        return self._create_dataloader(self._validation_ds)

    def test_dataloader(self) -> EVAL_DATALOADERS:
        if not hasattr(self, "_test_ds"):
            self.setup()
        return self._create_dataloader(self._test_ds)

    def _create_dataloader(self, dataset, **kwargs) -> DataLoader:
        return DataLoader(
            dataset,
            num_workers=self.num_workers,
            pin_memory=self.pin_memory,
            persistent_workers=self.persistent_workers,
            collate_fn=dataset.collate_fn,
            **kwargs,
        )


class _MockNevaDataset(Dataset):
    def __init__(
        self,
        tokenizer,
        image_processor,
        is_llava_next,
        name: str,
        num_samples: int,
        seq_length: int,
        seed: int = 42,
    ) -> None:
        super().__init__()
        self.name = name
        self.seq_length = seq_length

        self.vocab_size = tokenizer.vocab_size

        crop_size = image_processor.crop_size
        self.image_height, self.image_width = crop_size["height"], crop_size["width"]

        self.length = num_samples
        self.seed = seed

        self.loss_mask = torch.ones(self.seq_length, dtype=torch.float)
        self.position_ids = torch.arange(self.seq_length, dtype=torch.int64)
        self.tokenizer = tokenizer
        self.image_processor = image_processor
        self.is_llava_next = is_llava_next

    def __len__(self) -> int:
        return self.length

    def _get_text(self, idx: int) -> np.ndarray:
        np_gen = np.random.default_rng(seed=(self.seed + idx))
        return np_gen.integers(self.vocab_size, size=[self.seq_length], dtype=np.int64)

    def __getitem__(self, idx) -> Dict[str, torch.Tensor]:
        # Generate data of the expected size and datatype (based on GPTDataset).
        np_gen = np.random.default_rng(seed=(self.seed + idx))
        tokens = torch.from_numpy(np_gen.integers(self.vocab_size, size=[self.seq_length + 1], dtype=np.int64))
        tokens[2] = IMAGE_TOKEN_INDEX  # ImageToken token index
        labels = tokens.clone()
        images = torch.from_numpy(np_gen.random(size=[3, self.image_height, self.image_width], dtype=np.float32))
        tokens = tokens[:-1]
        labels = labels[1:]
        if not self.is_llava_next:
            return {
                "media": images,
                "tokens": tokens,
                "labels": labels,
                "loss_mask": self.loss_mask,
                "position_ids": self.position_ids,
            }
        else:
            #  attention_mask, image_sizes, num_media_tiles required for llava-next. Neva model will ignore these
            attention_mask = torch.ones(len(tokens), dtype=torch.long)
            image_sizes = torch.tensor([[self.image_height, self.image_width]], dtype=torch.long)
            image_array = self.image_processor.preprocess(images, return_tensors='pt', do_rescale=False)[
                'pixel_values'
            ][0]
            num_media_tiles = image_array.shape[0]
            return {
                "media": image_array,
                "tokens": tokens,
                "labels": labels,
                "loss_mask": self.loss_mask,
                "position_ids": self.position_ids,
                "image_sizes": image_sizes,
                "num_media_tiles": num_media_tiles,
                "attention_mask": attention_mask,
            }

    def _collate_fn(self, batch):
        """
        A default implementation of a collation function.
        Users should override this method to define custom data loaders.
        """
        collated_batch = data.dataloader.default_collate(batch)
        if not self.is_llava_next:
            collated_batch["attention_mask"] = None
        else:
            collated_batch['media'] = collated_batch['media'].contiguous().view(-1, *collated_batch['media'].shape[2:])
            collated_batch['image_sizes'] = (
                collated_batch['image_sizes'].contiguous().view(-1, *collated_batch['image_sizes'].shape[2:])
            )
        return collated_batch

    def collate_fn(self, batch):
        """Method that user pass as functor to DataLoader.

        The method optionally performs neural type checking and add types to the outputs.

        Please note, subclasses of Dataset should not implement `input_types`.

        # Usage:
        dataloader = torch.utils.data.DataLoader(
                ....,
                collate_fn=dataset.collate_fn,
                ....
        )

        Returns
        -------
            Collated batch, with or without types.
        """
        return self._collate_fn(batch)
