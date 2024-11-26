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


import functools
import json
import logging
import os
import re
import shutil
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import tensorstore  # This is important even though not used. Otherwise zarr raises error.
import torch
import yaml
import zarr
from tensorrt_llm._utils import np_bfloat16, str_dtype_to_torch
from torch.distributed.checkpoint import FileSystemReader
from torch.distributed.checkpoint.metadata import BytesStorageMetadata, TensorStorageMetadata
from torch.distributed.checkpoint.state_dict_loader import load_state_dict
from transformers import AutoTokenizer, PreTrainedTokenizer

from nemo.export.sentencepiece_tokenizer import SentencePieceTokenizer
from nemo.export.tarutils import TarPath, ZarrPathStore
from nemo.export.tiktoken_tokenizer import TiktokenTokenizer

LOGGER = logging.getLogger("NeMo")


def is_nemo_file(path):
    flag = False

    if path is not None:
        if len(path) > 5:
            pc = Path(path)
            if pc.exists():
                if pc.is_file():
                    if path[-5 : len(path)] == ".nemo":
                        flag = True

    return flag


class TarFileSystemReader(FileSystemReader):
    """Reader that accepts both Path and TarPath checkpoint directory.

    The FileSystemReader works with TarPath, but expects a pure Path.
    It's enough to skip the Path check in __init__.
    """

    def __init__(self, path: Union[Path, TarPath]) -> None:
        """Makes sure that super().__init__ gets a pure path as expected."""
        super_path = str(path) if isinstance(path, TarPath) else path
        super().__init__(super_path)
        if isinstance(path, TarPath):
            self.path = path  # overwrites path set in super().__init__ call


# Used only for the old, non-mcore path
def standarize_distributed_scaling_factors(state_dict: dict) -> dict:
    scales_dict = {k: v for k, v in state_dict.items() if 'extra_state' in k}
    state_dict = {k: v for k, v in state_dict.items() if 'extra_state' not in k}

    scales = {}

    for k, v in scales_dict.items():
        v.seek(0)
        scales[k + '.scale_fwd'] = torch.load(v)['scale_fwd'].cpu()

    combined_scales = {}
    for k in scales:
        if 'model.decoder.layers.' not in k:
            continue

        decomposed = k.split('.')
        combined = []
        id = 0
        decomposed[3] = str(id)
        while '.'.join(decomposed) in scales:
            combined.append(scales['.'.join(decomposed)])
            id += 1
            decomposed[3] = str(id)

        del decomposed[3]
        combined_scales['.'.join(decomposed)] = torch.stack(combined)

    return state_dict | combined_scales


def rename_extra_states(state_dict: dict) -> dict:
    mcore_extra_states = {}

    for key, value in state_dict.items():
        if 'extra_state' not in key:
            continue

        decomposed_sharded_key = key.split('/')
        if not len(decomposed_sharded_key):
            continue

        key_base, shard_key = decomposed_sharded_key
        if '_' not in shard_key:
            continue

        shard_layer = shard_key.split('_')[1]
        if not shard_layer.isnumeric():
            continue

        split_string = 'layers'
        decomposed_key = key_base.split(split_string)
        mcore_key = (f'{split_string}.{shard_layer}').join(decomposed_key)

        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        mcore_extra_states[mcore_key] = value

    state_dict = {k: v for k, v in state_dict.items() if 'extra_state' not in k}
    return state_dict | mcore_extra_states


def load_sharded_metadata_torch_dist(checkpoint_dir: Union[Path, TarPath], torch_tensor: bool = True):
    fs_reader = TarFileSystemReader(checkpoint_dir)
    metadata = fs_reader.read_metadata()

    state_dict = {
        k: torch.empty(tp.size, dtype=tp.properties.dtype)
        for k, tp in metadata.state_dict_metadata.items()
        if isinstance(tp, TensorStorageMetadata)
    }

    state_dict.update(
        {k: [] for k, tp in metadata.state_dict_metadata.items() if isinstance(tp, BytesStorageMetadata)}
    )

    load_state_dict(
        state_dict,
        storage_reader=fs_reader,
        no_dist=True,
    )
    state_dict = rename_extra_states(state_dict)

    if not torch_tensor:
        for k, v in state_dict.items():
            if v.dtype == torch.bfloat16:
                state_dict[k] = v.view(torch.int16).numpy().view(np_bfloat16)
            else:
                state_dict[k] = v.numpy()
    return state_dict


def get_sharded_file(dir: dict, layer_number: int) -> Optional[os.PathLike]:
    pt_file_list = list(dir.glob(f'shard_{layer_number}_*.pt'))
    if pt_file_list == []:
        return None
    return pt_file_list[0]


def load_sharded_pickle_extra_state_scale(dir: Union[Path, TarPath]):
    pt_files = list(dir.glob('shard_*_*.pt'))
    extra_states = {}
    for file in pt_files:
        shard_name = file.name.split('.')[0]
        with file.open('rb') as opened_file:
            extra_states[dir.name + '/' + shard_name] = torch.load(opened_file)

    return rename_extra_states(extra_states)


def contains_extra_states(subdir: Union[Path, TarPath]):
    return list(subdir.glob('shard_0_*.pt')) != []


def load_sharded_metadata_zarr(checkpoint_dir: Union[Path, TarPath], torch_tensor=True):
    sharded_state_dict = {}
    for subdir in checkpoint_dir.iterdir():
        if not subdir.is_dir():
            continue

        if contains_extra_states(subdir):
            sharded_state_dict |= load_sharded_pickle_extra_state_scale(subdir)
        elif (subdir / '.zarray').exists():
            key = subdir.name
            zstore = ZarrPathStore(subdir)
            arr = zarr.open(zstore, 'r')

            if torch_tensor:
                # sharded_state_dict[key] = torch.from_numpy(arr[:].astype("float32")).to(dtype=torch.bfloat16)
                if arr.dtype.name == "bfloat16":
                    sharded_state_dict[key] = torch.from_numpy(arr[:].view(np.int16)).view(torch.bfloat16)
                else:
                    sharded_state_dict[key] = torch.from_numpy(arr[:]).view(str_dtype_to_torch(arr.dtype.name))
            else:
                sharded_state_dict[key] = arr[:]

    return sharded_state_dict


def load_sharded_metadata(checkpoint_dir: Union[Path, TarPath], torch_tensor=True):
    with (checkpoint_dir / 'metadata.json').open(mode='r') as f:
        config_dict = json.load(f)
    if config_dict['sharded_backend'] == 'zarr':
        return load_sharded_metadata_zarr(checkpoint_dir, torch_tensor)
    elif config_dict['sharded_backend'] == 'torch_dist':
        return load_sharded_metadata_torch_dist(checkpoint_dir, torch_tensor)
    else:
        raise NotImplementedError(f'Distributed checkpoint backend {config_dict["sharded_backend"]} not supported')


def update_tokenizer_paths(tokenizer_config: Dict, unpacked_checkpoints_dir):
    def _update_config_entry(key, file_pattern):
        old_path = tokenizer_config.get(key, None)
        if old_path is None:
            return
        old_path = Path(old_path)
        new_path = unpacked_checkpoints_dir.get_tokenizer_file_path("tokenizer", key, file_pattern)
        if new_path:
            LOGGER.debug(f"Update tokenizer {key} {old_path} -> {new_path}")
            tokenizer_config[key] = new_path
        elif not old_path.exists():
            LOGGER.warning(f"Tokenizer {key}'s path {old_path} does not exists: set it to None")
            tokenizer_config[key] = None

    _update_config_entry("model", "*.model")
    _update_config_entry("vocab_file", "*vocab*")
    _update_config_entry("merge_file", "*merge*.txt")

    return tokenizer_config


def copy_tokenizer_files(config, out_dir):
    basenames = {
        "model": "tokenizer",
        "vocab_file": "vocab",
        "merge_file": "merges",
    }

    for key in basenames.keys():
        if config.get(key, None) is None:
            continue

        path = config[key]

        if isinstance(path, str):
            path = Path(path)

        if not path.exists():
            LOGGER.debug(f"Tokenizer {key}: {path} file not found")
            continue

        dst_path = out_dir / f"{basenames[key]}{path.suffix}"
        config[key] = str(dst_path)
        LOGGER.debug(f"Copy tokenizer {key}: {path}->{dst_path}")

        # Copy 'path' to 'dst_path' without shutil.copy(...) because 'path' may be a TarPath
        with path.open('rb') as infile:
            with open(dst_path, 'wb') as outfile:
                outfile.write(infile.read())

    return config


def get_tokenizer(tokenizer_dir_or_path: Union[str, Path]) -> PreTrainedTokenizer:
    """Loads the tokenizer from the decoded NeMo weights dir."""
    tokenizer_dir_or_path = Path(tokenizer_dir_or_path)
    if (tokenizer_dir_or_path / "nemo_context").exists():
        from nemo.lightning import io

        tokenizer_spec = io.load_context((tokenizer_dir_or_path / "nemo_context"), subpath="model.tokenizer")
        return build_tokenizer(tokenizer_spec)
    elif os.path.exists(os.path.join(tokenizer_dir_or_path, "vocab.json")):
        vocab_path = tokenizer_dir_or_path / "vocab.json" if tokenizer_dir_or_path.is_dir() else tokenizer_dir_or_path
        tokenizer_config = {"library": "tiktoken", "vocab_file": str(vocab_path)}
        return build_tokenizer(tokenizer_config)
    else:
        if (tokenizer_dir_or_path / "huggingface_tokenizer").is_dir():
            return AutoTokenizer.from_pretrained(tokenizer_dir_or_path / "huggingface_tokenizer")

        model_path = (
            tokenizer_dir_or_path / "tokenizer.model" if tokenizer_dir_or_path.is_dir() else tokenizer_dir_or_path
        )
        tokenizer_config = {"library": "sentencepiece", "model": str(model_path)}
        return build_tokenizer(tokenizer_config)


def build_tokenizer(tokenizer):
    if isinstance(tokenizer, dict):
        tokenizer_config = tokenizer
        if tokenizer_config["library"] == "sentencepiece":
            return SentencePieceTokenizer(model_path=tokenizer_config["model"])
        elif tokenizer_config["library"] == "tiktoken":
            return TiktokenTokenizer(vocab_file=tokenizer_config["vocab_file"])
        elif "GPT2" in tokenizer_config["type"]:
            tokenizer = GPT2Tokenizer(tokenizer_config["vocab_file"], tokenizer_config["merge_file"])
        else:
            raise ValueError(f'Tokenizer type {tokenizer_config["library"]} not handled')

        if tokenizer.bos_token_id is None:
            tokenizer.add_special_tokens({"bos_token": "<s>"})
        if tokenizer.eos_token_id is None:
            tokenizer.add_special_tokens({"eos_token": "</s>"})
    else:
        # For NeMo tokenizers, monkey patch encode & batch_decode methods for unified interface
        import nemo.collections.common.tokenizers as nemo_tokenizers

        if isinstance(tokenizer, nemo_tokenizers.TokenizerSpec):
            if isinstance(tokenizer, nemo_tokenizers.AutoTokenizer):
                # Unwrap the original methods of HF tokenizer
                batch_decode = tokenizer.tokenizer.batch_decode
                encode = tokenizer.tokenizer.encode
            elif isinstance(tokenizer, nemo_tokenizers.SentencePieceTokenizer):
                # Define HF equivalents based on available SP methods
                def batch_decode(self, ids):
                    if torch.is_tensor(ids):
                        ids = ids.cpu().numpy()
                    if isinstance(ids, np.ndarray):
                        ids = ids.tolist()
                    return self.tokenizer.decode(ids)

                encode = tokenizer.tokenizer.encode_as_ids
            else:
                raise NotImplementedError(f"Patching tokenizer methods for {type(tokenizer)} is not available")

            tokenizer.bos_token_id = tokenizer.bos_id
            tokenizer.eos_token_id = tokenizer.eos_id
            nemo_tokenizers.TokenizerSpec.encode = encode
            nemo_tokenizers.TokenizerSpec.batch_decode = batch_decode

    return tokenizer


def load_nemo_model(nemo_ckpt: Union[str, Path], nemo_export_dir: Union[str, Path], mcore_scales_format: bool = True):

    if not os.path.exists(nemo_ckpt):
        raise TypeError("%s does not exist", nemo_ckpt)

    if os.path.isdir(nemo_ckpt):
        nemo_dir = Path(nemo_ckpt)
    else:
        nemo_dir = TarPath(nemo_ckpt)

    tokenizer = None
    try:
        unpacked_checkpoint_dir = UnpackedNemoCheckpointDir(nemo_dir, load_checkpoints_to_cpu=True)

        if (nemo_dir / "model_weights").exists():
            dist_ckpt_folder = nemo_dir / "model_weights"

            model = load_sharded_metadata(dist_ckpt_folder)
            if not mcore_scales_format:
                model |= {k: v[0] for k, v in model.items() if 'extra_state' in k and isinstance(v, list)}
                model = standarize_distributed_scaling_factors(model)

            nemo_model_config = unpacked_checkpoint_dir.model_config

            if nemo_model_config["tokenizer"].get("library", None) == "huggingface":
                tokenizer = AutoTokenizer.from_pretrained(
                    nemo_model_config["tokenizer"]["type"],
                    use_fast=nemo_model_config["tokenizer"].get("use_fast", False),
                )
            else:
                tokenizer_config = update_tokenizer_paths(nemo_model_config["tokenizer"], unpacked_checkpoint_dir)
                tokenizer_config = copy_tokenizer_files(tokenizer_config, nemo_export_dir)

                tokenizer = build_tokenizer(tokenizer_config)
        elif (nemo_dir / "weights").exists():
            dist_ckpt_folder = nemo_dir / "weights"
            model = load_sharded_metadata(dist_ckpt_folder)
            io_folder = nemo_dir / "context"

            if (io_folder / "model.yaml").exists():
                with open(io_folder / "model.yaml", 'r') as stream:
                    config = yaml.safe_load(stream)

                nemo_model_config = {}
                for k, v in config["config"].items():
                    if isinstance(v, (float, int, str, bool)):
                        nemo_model_config[k] = v
                    elif k == "activation_func":
                        nemo_model_config["activation"] = v["_target_"].rsplit('.', 1)[-1]
            else:
                from nemo.lightning import io

                config = io.load_context(io_folder, subpath="model.config")

                nemo_model_config = {}
                for k, v in config.__dict__.items():
                    if isinstance(v, (float, int, str, bool)):
                        nemo_model_config[k] = v
                    elif k == "activation_func":
                        if isinstance(v, torch.jit.ScriptFunction):
                            nemo_model_config["activation"] = v.name
                        else:
                            nemo_model_config["activation"] = v.__name__

            if nemo_model_config.get("num_moe_experts") is None:
                nemo_model_config["num_moe_experts"] = 0
                nemo_model_config["moe_router_topk"] = 0
            if nemo_model_config["activation"] == "silu":
                nemo_model_config["activation"] = "fast-swiglu"
            elif nemo_model_config["activation"] == "openai_gelu":
                nemo_model_config["activation"] = "openai-gelu"
            elif nemo_model_config["activation"] == "squared_relu":
                nemo_model_config["activation"] = "squared-relu"

            nemo_model_config["mcore_gpt"] = True
            nemo_model_config["max_position_embeddings"] = nemo_model_config.get("seq_length", 4096)
            nemo_model_config["rotary_percentage"] = nemo_model_config.get("rotary_percent", 1.0)

            shutil.copytree(io_folder, nemo_export_dir / "nemo_context")
        else:
            raise Exception("Not a supported NeMo file format: only distributed MCore NeMo checkpoints are supported.")
    finally:
        if isinstance(nemo_dir, TarPath):
            nemo_dir.tarobject.close()

    return model, nemo_model_config, tokenizer


def cpu_map_location(storage, loc):
    return storage.cpu()


def gpu_map_location(storage, loc):
    if loc.startswith("cuda"):
        training_gpu_idx = int(loc.split(":")[1])
        inference_gpu_idx = training_gpu_idx % torch.cuda.device_count()
        return storage.cuda(inference_gpu_idx)
    elif loc.startswith("cpu"):
        return storage.cpu()
    else:
        raise ValueError(f"Not handled {loc}")


class UnpackedNemoCheckpointDir:
    def __init__(
        self,
        checkpoints_dir: Union[Path, TarPath],
        load_checkpoints_to_cpu: bool = False,
    ):
        assert isinstance(checkpoints_dir, (Path, TarPath))
        self._checkpoints_dir = checkpoints_dir
        self._load_checkpoints_to_cpu = load_checkpoints_to_cpu

    @property
    @functools.lru_cache
    def model_config(self):
        model_config = None

        model_config_filename = "model_config.yaml"
        model_configs_paths = list(self._checkpoints_dir.rglob(model_config_filename))
        if model_configs_paths:
            if len(model_configs_paths) > 1:
                LOGGER.debug(f"There are more than single {model_config_filename} in" f" {self._checkpoints_dir}")
            model_config_path = model_configs_paths[0]
            LOGGER.debug("Loading model config from %s", model_config_path)
            with model_config_path.open("r") as model_config_file:
                model_config = yaml.load(model_config_file, Loader=yaml.SafeLoader)
        else:
            LOGGER.debug("Searching model config in checkpoints")
            # try to obtain from checkpoint
            checkpoint_name = self.checkpoint_name
            checkpoints_paths = sorted(self._checkpoints_dir.rglob(checkpoint_name))
            if checkpoints_paths:
                # assume that parallel ranks 0 checkpoint should have model config embedded
                checkpoint_path = checkpoints_paths[0]

                map_location_fn = cpu_map_location if self._load_checkpoints_to_cpu else gpu_map_location

                model_00 = torch.load(checkpoint_path, map_location=map_location_fn)
                if "hyper_parameters" in model_00 and "cfg" in model_00["hyper_parameters"]:
                    model_config = model_00["hyper_parameters"]["cfg"]
                    LOGGER.debug("Loaded model config from checkpoint %s", checkpoint_path)
                else:
                    LOGGER.debug("Could not find model config in checkpoint %s", checkpoint_path)

                del model_00

        if model_config is None:
            LOGGER.warning("Could not find checkpoint with NeMo model config in %s", self._checkpoints_dir)

        LOGGER.debug("Loaded model config %s", model_config)

        return model_config

    @property
    def checkpoints_dir(self):
        return self._checkpoints_dir

    def get_checkpoints_paths(self, tensor_model_parallel_size=1, pipeline_model_parallel_size=1):
        """Injects tensor/pipeline model parallel ranks into the filepath.
        Does nothing if not using model parallelism.
        """
        checkpoint_path_without_rank = self.checkpoints_dir / self.checkpoint_name

        def _inject_parallel_ranks(tp_rank, pp_rank):
            if tensor_model_parallel_size > 1 or pipeline_model_parallel_size > 1:
                if pipeline_model_parallel_size is None or pipeline_model_parallel_size == 1:
                    checkpoint_path = (
                        checkpoint_path_without_rank.parent
                        / f"mp_rank_{tp_rank:02d}"
                        / checkpoint_path_without_rank.name
                    )
                else:
                    checkpoint_path = (
                        checkpoint_path_without_rank.parent
                        / f"tp_rank_{tp_rank:02d}_pp_rank_{pp_rank:03d}"
                        / checkpoint_path_without_rank.name
                    )
                return checkpoint_path
            else:
                return checkpoint_path_without_rank

        return [
            [
                _inject_parallel_ranks(tp_rank=tp_rank, pp_rank=pp_rank)
                for pp_rank in range(pipeline_model_parallel_size)
            ]
            for tp_rank in range(tensor_model_parallel_size)
        ]

    @property
    @functools.lru_cache
    def checkpoint_name(self):
        patterns = [
            "model_weights.ckpt",  # older megatron checkpoints
            "*last.ckpt",  # newer format of checkpoints
        ]
        for pattern in patterns:
            model_files = sorted(list(self._checkpoints_dir.rglob(pattern)))
            if model_files:
                return model_files[0].name

        raise ValueError(f"Could not find checkpoint files in {self._checkpoints_dir}")

    @functools.lru_cache
    def get_tokenizer_file_path(self, tokenizer_key, file_key, default_filename_pattern):
        model_config = self.model_config
        file_property = None
        if tokenizer_key in model_config and file_key in model_config[tokenizer_key]:
            file_property = model_config[tokenizer_key][file_key]
        elif file_key in model_config:
            file_property = model_config[file_key]

        LOGGER.debug("model_config[%s][%s]=%s", tokenizer_key, file_key, file_property)

        if file_property and file_property.startswith("nemo:"):
            filename = file_property.split("nemo:")[1]
            filename_pattern = f"*{filename}"
        elif file_property and file_property.startswith("/artifacts/"):
            filename = Path(file_property).name
            filename_pattern = f"*{filename}"
        elif file_property is None or file_property == "None":
            filename_pattern = None
        else:
            filename_pattern = default_filename_pattern
            LOGGER.warning(
                f"Tokenizer file from config: {tokenizer_key}.{file_key}={file_property} "
                f"looks like unsupported path. Pattern {filename_pattern} will be used."
            )

        file_path = None
        if filename_pattern is not None:
            files_paths = list(self._checkpoints_dir.glob(filename_pattern))
            if files_paths:
                assert len(files_paths) == 1
                file_path = files_paths[0]

        return file_path
