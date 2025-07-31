#!/usr/bin/env python3
"""
FIXED: BLIP3-o Dataset with Consistent Processing for Training and Evaluation
Key fixes:
1. Disabled statistics collection that caused inconsistencies
2. Consistent data processing between train and eval
3. Simplified collate function with target-based noise scaling
4. Better debugging and monitoring
"""

import torch
from torch.utils.data import Dataset, DataLoader, IterableDataset
import pickle
import numpy as np
from typing import Dict, Any, Optional, List, Tuple, Union, Iterator
from pathlib import Path
import logging
import json
import random
import time
import gc
import torch.nn.functional as F

logger = logging.getLogger(__name__)


class BLIP3oCLIPReproductionDataset(IterableDataset):
    """
    FIXED: Dataset for CLIP reproduction with consistent processing
    
    This dataset loads:
    - CLIP embeddings [B, N, 1024] as TARGET (what we want to reproduce)
    - EVA embeddings [B, N, 4096] as CONDITIONING (guidance)
    
    FIXED: Removed statistics collection and ensured consistent processing
    """
    
    def __init__(
        self,
        chunked_embeddings_dir: Union[str, Path],
        split: str = "train",
        training_mode: str = "patch_only",
        normalize_embeddings: bool = False,  # FIXED: Keep False for consistency
        max_shards: Optional[int] = None,
        shuffle_shards: bool = True,
        shuffle_within_shard: bool = True,
        expected_tokens: Optional[int] = None,
        # Error handling
        skip_corrupted: bool = True,
        validate_shapes: bool = True,
        max_retries: int = 3,
        # FIXED: Remove statistics collection for consistency
        collect_statistics: bool = False,
    ):
        super().__init__()
        
        self.chunked_embeddings_dir = Path(chunked_embeddings_dir)
        self.split = split
        self.training_mode = training_mode
        self.normalize_embeddings = normalize_embeddings
        self.max_shards = max_shards
        self.shuffle_shards = shuffle_shards
        self.shuffle_within_shard = shuffle_within_shard
        self.skip_corrupted = skip_corrupted
        self.validate_shapes = validate_shapes
        self.max_retries = max_retries
        self.collect_statistics = collect_statistics  # FIXED: Usually False
        
        # Determine expected tokens
        if expected_tokens is None:
            self.expected_tokens = 257 if training_mode == "cls_patch" else 256
        else:
            self.expected_tokens = expected_tokens
        
        # Setup random state
        self.rng = random.Random(42)
        
        # FIXED: Simplified statistics (no adaptive updates)
        self.data_statistics = {
            'clip_norm_mean': 0.0,
            'eva_norm_mean': 0.0,
            'samples_seen': 0
        }
        
        # Load manifest and prepare shards
        self._load_manifest()
        self._prepare_shard_list()
        
        # Current state
        self.current_shard_idx = 0
        self.current_shard_data = None
        self.current_sample_idx = 0
        self.total_samples_processed = 0
        
        # Calculate estimated length for __len__ method
        self._estimate_length()
        
        logger.info(f"FIXED CLIP Reproduction Dataset initialized:")
        logger.info(f"  Directory: {self.chunked_embeddings_dir}")
        logger.info(f"  Mode: {self.training_mode} ({self.expected_tokens} tokens)")
        logger.info(f"  TARGET: CLIP embeddings [B, N, 1024]")
        logger.info(f"  CONDITIONING: EVA embeddings [B, N, 4096]")
        logger.info(f"  Normalize: {self.normalize_embeddings}")
        logger.info(f"  Shards: {len(self.shard_files) if hasattr(self, 'shard_files') else 'Unknown'}")
        logger.info(f"  Collect statistics: {self.collect_statistics}")

    def _load_manifest(self):
        """Load embeddings manifest"""
        manifest_path = self.chunked_embeddings_dir / "embeddings_manifest.json"
        
        try:
            if manifest_path.exists():
                with open(manifest_path, 'r') as f:
                    self.manifest = json.load(f)
                logger.info(f"Loaded manifest: {self.manifest.get('total_shards', 0)} shards, {self.manifest.get('total_samples', 0):,} samples")
            else:
                self.manifest = {"total_shards": 0, "total_samples": 0}
                logger.warning(f"No manifest found at {manifest_path}")
        except Exception as e:
            logger.warning(f"Failed to load manifest: {e}")
            self.manifest = {"total_shards": 0, "total_samples": 0}

    def _prepare_shard_list(self):
        """Prepare list of shard files"""
        # Look for shard files
        mode_suffix = "cls_patch" if self.training_mode == "cls_patch" else "patch_only"
        patterns = [
            f"embeddings_shard_*_{mode_suffix}.pkl",
            f"*_{mode_suffix}.pkl",
            "embeddings_shard_*.pkl",
            "*.pkl"
        ]
        
        shard_files = []
        for pattern in patterns:
            shard_files = list(self.chunked_embeddings_dir.glob(pattern))
            if shard_files:
                logger.info(f"Found {len(shard_files)} files with pattern: {pattern}")
                break
        
        if not shard_files:
            raise FileNotFoundError(f"No shard files found in {self.chunked_embeddings_dir}")
        
        # Sort files
        shard_files.sort(key=lambda x: int(''.join(filter(str.isdigit, x.stem))) if any(c.isdigit() for c in x.stem) else 0)
        
        # Apply max shards limit
        if self.max_shards is not None:
            shard_files = shard_files[:self.max_shards]
        
        # Filter existing files
        self.shard_files = [f for f in shard_files if f.exists()]
        
        if self.shuffle_shards:
            self.rng.shuffle(self.shard_files)
        
        logger.info(f"Prepared {len(self.shard_files)} shard files")

    def _estimate_length(self):
        """Estimate total number of samples for __len__ method"""
        try:
            # If manifest is available, use it
            if self.manifest.get('total_samples', 0) > 0:
                manifest_samples = self.manifest['total_samples']
                # Adjust for max_shards limitation
                if self.max_shards is not None and self.manifest.get('total_shards', 0) > 0:
                    ratio = min(self.max_shards / self.manifest['total_shards'], 1.0)
                    self.estimated_length = int(manifest_samples * ratio)
                else:
                    self.estimated_length = manifest_samples
                logger.info(f"Using manifest for length estimation: {self.estimated_length:,} samples")
                return
            
            # Fallback: try to load first shard to estimate
            if self.shard_files:
                try:
                    first_shard = self._load_shard(self.shard_files[0])
                    if first_shard and 'clip_blip3o_embeddings' in first_shard:
                        samples_per_shard = first_shard['clip_blip3o_embeddings'].shape[0]
                        self.estimated_length = samples_per_shard * len(self.shard_files)
                        logger.info(f"Estimated length from first shard: {self.estimated_length:,} samples ({samples_per_shard} per shard)")
                        return
                except Exception as e:
                    logger.warning(f"Could not load first shard for estimation: {e}")
            
            # Final fallback: rough estimate
            self.estimated_length = len(self.shard_files) * 1000  # Assume 1000 samples per shard
            logger.warning(f"Using rough length estimate: {self.estimated_length:,} samples")
            
        except Exception as e:
            logger.warning(f"Length estimation failed: {e}")
            self.estimated_length = 1000  # Very conservative fallback

    def __len__(self) -> int:
        """Return estimated length for DataLoader compatibility"""
        return self.estimated_length

    def _update_statistics(self, clip_emb: torch.Tensor, eva_emb: torch.Tensor):
        """FIXED: Simplified statistics update (optional)"""
        if not self.collect_statistics:
            return
            
        with torch.no_grad():
            batch_size = clip_emb.shape[0]
            
            # Simple statistics
            clip_norm_mean = torch.norm(clip_emb, dim=-1).mean().item()
            eva_norm_mean = torch.norm(eva_emb, dim=-1).mean().item()
            
            # Simple average (no complex EMA)
            if self.data_statistics['samples_seen'] == 0:
                self.data_statistics['clip_norm_mean'] = clip_norm_mean
                self.data_statistics['eva_norm_mean'] = eva_norm_mean
            else:
                # Simple running average
                n = self.data_statistics['samples_seen']
                self.data_statistics['clip_norm_mean'] = (self.data_statistics['clip_norm_mean'] * n + clip_norm_mean * batch_size) / (n + batch_size)
                self.data_statistics['eva_norm_mean'] = (self.data_statistics['eva_norm_mean'] * n + eva_norm_mean * batch_size) / (n + batch_size)
            
            self.data_statistics['samples_seen'] += batch_size

    def _load_shard(self, shard_path: Path) -> Optional[Dict[str, Any]]:
        """Load a single shard with error handling"""
        for attempt in range(self.max_retries):
            try:
                with open(shard_path, 'rb') as f:
                    shard_data = pickle.load(f)
                
                # Validate and process shard
                self._validate_and_process_shard(shard_data, shard_path)
                
                return shard_data
                
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}/{self.max_retries} failed for {shard_path}: {e}")
                if attempt == self.max_retries - 1:
                    if self.skip_corrupted:
                        logger.warning(f"Skipping corrupted shard: {shard_path}")
                        return None
                    else:
                        raise
                time.sleep(0.1)

    def _validate_and_process_shard(self, shard_data: Dict[str, Any], shard_path: Path):
        """Validate and process shard data"""
        # Check required keys
        required_keys = ['clip_blip3o_embeddings', 'eva_blip3o_embeddings', 'captions']
        for key in required_keys:
            if key not in shard_data:
                raise ValueError(f"Missing key '{key}' in shard {shard_path}")
        
        # Get embeddings
        clip_emb = shard_data['clip_blip3o_embeddings']
        eva_emb = shard_data['eva_blip3o_embeddings']
        
        # Convert to tensors if needed
        if not torch.is_tensor(clip_emb):
            clip_emb = torch.tensor(clip_emb, dtype=torch.float32)
            shard_data['clip_blip3o_embeddings'] = clip_emb
        if not torch.is_tensor(eva_emb):
            eva_emb = torch.tensor(eva_emb, dtype=torch.float32)
            shard_data['eva_blip3o_embeddings'] = eva_emb
        
        # Validate shapes
        if self.validate_shapes:
            if clip_emb.dim() != 3 or eva_emb.dim() != 3:
                raise ValueError(f"Expected 3D tensors, got CLIP: {clip_emb.shape}, EVA: {eva_emb.shape}")
            
            if clip_emb.shape[0] != eva_emb.shape[0]:
                raise ValueError(f"Batch size mismatch: CLIP {clip_emb.shape[0]} vs EVA {eva_emb.shape[0]}")
            
            clip_tokens, eva_tokens = clip_emb.shape[1], eva_emb.shape[1]
            if clip_tokens != eva_tokens:
                raise ValueError(f"Token count mismatch: CLIP {clip_tokens} vs EVA {eva_tokens}")
        
        # Handle token count adaptation
        current_tokens = clip_emb.shape[1]
        if current_tokens != self.expected_tokens:
            logger.debug(f"Adapting from {current_tokens} to {self.expected_tokens} tokens")
            
            if current_tokens == 256 and self.expected_tokens == 257:
                # Add CLS token (average of patches)
                clip_cls = clip_emb.mean(dim=1, keepdim=True)
                eva_cls = eva_emb.mean(dim=1, keepdim=True)
                shard_data['clip_blip3o_embeddings'] = torch.cat([clip_cls, clip_emb], dim=1)
                shard_data['eva_blip3o_embeddings'] = torch.cat([eva_cls, eva_emb], dim=1)
            elif current_tokens == 257 and self.expected_tokens == 256:
                # Remove CLS token
                shard_data['clip_blip3o_embeddings'] = clip_emb[:, 1:, :]
                shard_data['eva_blip3o_embeddings'] = eva_emb[:, 1:, :]
            else:
                raise ValueError(f"Cannot adapt from {current_tokens} to {self.expected_tokens} tokens")
        
        # Update statistics BEFORE normalization (if enabled)
        if self.collect_statistics:
            self._update_statistics(
                shard_data['clip_blip3o_embeddings'], 
                shard_data['eva_blip3o_embeddings']
            )
        
        # Apply normalization if requested (usually disabled)
        if self.normalize_embeddings:
            shard_data = self._normalize_embeddings(shard_data)

    def _normalize_embeddings(self, shard_data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply L2 normalization to embeddings (usually disabled)"""
        clip_emb = shard_data['clip_blip3o_embeddings']
        eva_emb = shard_data['eva_blip3o_embeddings']
        
        # Check for NaN/Inf
        if torch.isnan(clip_emb).any() or torch.isinf(clip_emb).any():
            logger.warning("Found NaN/Inf in CLIP embeddings")
            clip_emb = torch.nan_to_num(clip_emb, nan=0.0, posinf=1.0, neginf=-1.0)
        
        if torch.isnan(eva_emb).any() or torch.isinf(eva_emb).any():
            logger.warning("Found NaN/Inf in EVA embeddings")
            eva_emb = torch.nan_to_num(eva_emb, nan=0.0, posinf=1.0, neginf=-1.0)
        
        # Apply L2 normalization
        eps = 1e-8
        clip_normalized = F.normalize(clip_emb + eps, p=2, dim=-1)
        eva_normalized = F.normalize(eva_emb + eps, p=2, dim=-1)
        
        shard_data['clip_blip3o_embeddings'] = clip_normalized
        shard_data['eva_blip3o_embeddings'] = eva_normalized
        shard_data['normalization_applied'] = True
        
        return shard_data

    def _load_next_shard(self) -> bool:
        """Load next shard"""
        # Cleanup previous shard
        if self.current_shard_data is not None:
            del self.current_shard_data
            gc.collect()
        
        # Check if more shards available
        if self.current_shard_idx >= len(self.shard_files):
            self.current_shard_data = None
            return False
        
        # Try to load next shard
        while self.current_shard_idx < len(self.shard_files):
            shard_path = self.shard_files[self.current_shard_idx]
            
            self.current_shard_data = self._load_shard(shard_path)
            
            if self.current_shard_data is not None:
                # Prepare samples
                num_samples = self.current_shard_data['clip_blip3o_embeddings'].shape[0]
                self.current_samples = list(range(num_samples))
                
                if self.shuffle_within_shard:
                    self.rng.shuffle(self.current_samples)
                
                self.current_sample_idx = 0
                
                logger.info(f"Loaded shard {self.current_shard_idx + 1}/{len(self.shard_files)}: {num_samples} samples")
                self.current_shard_idx += 1
                return True
            else:
                self.current_shard_idx += 1
                continue
        
        self.current_shard_data = None
        return False

    def get_data_statistics(self) -> Dict[str, float]:
        """Get collected data statistics"""
        return self.data_statistics.copy()

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        """Iterate through all samples"""
        self.current_shard_idx = 0
        self.current_shard_data = None
        self.current_sample_idx = 0
        self.total_samples_processed = 0
        
        logger.debug(f"Starting iteration over {len(self.shard_files)} shards")
        
        if not self._load_next_shard():
            return
        
        while self.current_shard_data is not None:
            while self.current_sample_idx < len(self.current_samples):
                try:
                    sample_idx = self.current_samples[self.current_sample_idx]
                    
                    # Extract sample data
                    clip_emb = self.current_shard_data['clip_blip3o_embeddings'][sample_idx]
                    eva_emb = self.current_shard_data['eva_blip3o_embeddings'][sample_idx]
                    caption = self.current_shard_data['captions'][sample_idx]
                    
                    # Final validation
                    if self.validate_shapes:
                        if clip_emb.shape != (self.expected_tokens, 1024):
                            raise ValueError(f"Invalid CLIP shape: {clip_emb.shape}")
                        if eva_emb.shape != (self.expected_tokens, 4096):
                            raise ValueError(f"Invalid EVA shape: {eva_emb.shape}")
                    
                    # Check for NaN/Inf
                    if torch.isnan(clip_emb).any() or torch.isnan(eva_emb).any():
                        if self.skip_corrupted:
                            self.current_sample_idx += 1
                            continue
                        else:
                            raise ValueError("NaN detected in embeddings")
                    
                    # Create sample item for CLIP reproduction
                    item = {
                        'eva_embeddings': eva_emb,      # [N, 4096] - CONDITIONING
                        'clip_embeddings': clip_emb,    # [N, 1024] - TARGET to reproduce
                        'caption': caption,
                        'key': f"shard_{self.current_shard_idx-1}_sample_{sample_idx}",
                        'sample_idx': sample_idx,
                        'training_mode': self.training_mode,
                        'num_tokens': self.expected_tokens,
                        'normalized': self.current_shard_data.get('normalization_applied', False),
                    }
                    
                    self.current_sample_idx += 1
                    self.total_samples_processed += 1
                    
                    yield item
                    
                except Exception as e:
                    if self.skip_corrupted:
                        logger.warning(f"Skipping corrupted sample {sample_idx}: {e}")
                        self.current_sample_idx += 1
                        continue
                    else:
                        raise
            
            if not self._load_next_shard():
                break
        
        logger.info(f"Iteration completed: {self.total_samples_processed} samples processed")


def clip_reproduction_collate_fn(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    FIXED: Collate function for CLIP reproduction with consistent noise handling
    
    This function:
    1. Takes clean CLIP embeddings as targets
    2. Uses EVA embeddings for conditioning
    3. Creates noise scaled to target distribution for consistency
    4. Focuses on clean data preparation
    """
    if not batch:
        raise ValueError("Empty batch")
    
    # Filter valid items
    valid_batch = [item for item in batch if item is not None]
    if not valid_batch:
        raise ValueError("No valid items in batch")
    
    try:
        # Stack embeddings
        eva_embeddings = torch.stack([item['eva_embeddings'] for item in valid_batch])     # [B, N, 4096]
        clip_embeddings = torch.stack([item['clip_embeddings'] for item in valid_batch])   # [B, N, 1024]
        
        # Collect metadata
        captions = [item['caption'] for item in valid_batch]
        keys = [item['key'] for item in valid_batch]
        
        batch_size, seq_len, clip_dim = clip_embeddings.shape
        device = clip_embeddings.device
        dtype = clip_embeddings.dtype
        
        # Ensure float32 for stability
        eva_embeddings = eva_embeddings.float()
        clip_embeddings = clip_embeddings.float()
        
        # Sample random timesteps for each sample in batch
        timesteps = torch.rand(batch_size, device=device, dtype=dtype)
        
        # FIXED: Create noise scaled to target distribution for consistency
        target_std = clip_embeddings.std()
        noise = torch.randn_like(clip_embeddings, device=device, dtype=dtype) * target_std
        
        # Linear interpolation for rectified flow: x_t = (1-t) * noise + t * clip_clean
        t_expanded = timesteps.view(batch_size, 1, 1)  # [B, 1, 1]
        noisy_clip = (1 - t_expanded) * noise + t_expanded * clip_embeddings
        
        # Velocity target: v = clip_clean - noise (for rectified flow)
        velocity_target = clip_embeddings - noise
        
        # Validation
        assert eva_embeddings.shape == (batch_size, seq_len, 4096), f"EVA shape: {eva_embeddings.shape}"
        assert clip_embeddings.shape == (batch_size, seq_len, 1024), f"CLIP shape: {clip_embeddings.shape}"
        assert noisy_clip.shape == (batch_size, seq_len, 1024), f"Noisy CLIP shape: {noisy_clip.shape}"
        assert velocity_target.shape == (batch_size, seq_len, 1024), f"Velocity target shape: {velocity_target.shape}"
        assert timesteps.shape == (batch_size,), f"Timesteps shape: {timesteps.shape}"
        
        return {
            # Model inputs
            'encoder_hidden_states': eva_embeddings,     # [B, N, 4096] - EVA conditioning
            'hidden_states': noisy_clip,                 # [B, N, 1024] - Noisy CLIP input
            'timestep': timesteps,                       # [B] - Flow matching timesteps
            
            # Training targets
            'clip_embeddings': clip_embeddings,          # [B, N, 1024] - Clean CLIP (target)
            'velocity_target': velocity_target,          # [B, N, 1024] - Velocity for flow matching
            'noise': noise,                              # [B, N, 1024] - Noise used
            
            # Metadata
            'captions': captions,
            'keys': keys,
            'batch_size': batch_size,
            'training_mode': valid_batch[0]['training_mode'],
            'num_tokens': valid_batch[0]['num_tokens'],
            'seq_len': seq_len,
            
            # FIXED: Data characteristics for debugging
            'eva_embeddings_normalized': False,
            'clip_embeddings_normalized': False,
            'eva_norm_mean': torch.norm(eva_embeddings, dim=-1).mean().item(),
            'clip_norm_mean': torch.norm(clip_embeddings, dim=-1).mean().item(),
            'noise_norm_mean': torch.norm(noise, dim=-1).mean().item(),
            'target_std_used': target_std.item(),
            'noise_scale_ratio': torch.norm(noise, dim=-1).mean().item() / torch.norm(clip_embeddings, dim=-1).mean().item(),
        }
        
    except Exception as e:
        logger.error(f"Error in collate function: {e}")
        logger.error(f"Batch size: {len(batch)}")
        if batch:
            try:
                logger.error(f"First item keys: {list(batch[0].keys())}")
                for key, value in batch[0].items():
                    if torch.is_tensor(value):
                        logger.error(f"  {key}: {value.shape} {value.dtype}")
            except:
                pass
        raise


def create_clip_reproduction_dataloaders(
    chunked_embeddings_dir: Union[str, Path],
    batch_size: int = 16,
    eval_batch_size: Optional[int] = None,
    training_mode: str = "patch_only",
    max_shards: Optional[int] = None,
    normalize_embeddings: bool = False,  # FIXED: Keep False
    num_workers: int = 0,
    pin_memory: bool = False,
    # FIXED: Disable statistics collection by default
    collect_statistics: bool = False,
    **kwargs
) -> Tuple[DataLoader, Optional[DataLoader]]:
    """FIXED: Create dataloaders for CLIP reproduction with consistent processing"""
    
    if eval_batch_size is None:
        eval_batch_size = batch_size
    
    logger.info(f"FIXED: Creating CLIP reproduction dataloaders:")
    logger.info(f"  Target: CLIP embeddings [B, N, 1024]")
    logger.info(f"  Conditioning: EVA embeddings [B, N, 4096]")
    logger.info(f"  Normalize: {normalize_embeddings}")
    logger.info(f"  Collect statistics: {collect_statistics}")
    
    # FIXED: Use identical settings for both train and eval
    dataset_kwargs = {
        'chunked_embeddings_dir': chunked_embeddings_dir,
        'training_mode': training_mode,
        'normalize_embeddings': normalize_embeddings,
        'max_shards': max_shards,
        'collect_statistics': collect_statistics,
        **kwargs
    }
    
    # Create training dataset
    train_dataset = BLIP3oCLIPReproductionDataset(
        split="train",
        shuffle_shards=True,
        shuffle_within_shard=True,
        **dataset_kwargs
    )
    
    # Create training dataloader
    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        collate_fn=clip_reproduction_collate_fn,
        pin_memory=pin_memory,
        drop_last=True,
        persistent_workers=num_workers > 0,
    )
    
    # FIXED: Create evaluation dataset with identical settings but no shuffling
    eval_dataset = BLIP3oCLIPReproductionDataset(
        split="eval",
        shuffle_shards=False,  # No shuffling for consistent evaluation
        shuffle_within_shard=False,
        **dataset_kwargs
    )
    
    eval_dataloader = DataLoader(
        eval_dataset,
        batch_size=eval_batch_size,
        num_workers=min(num_workers, 1),
        collate_fn=clip_reproduction_collate_fn,
        pin_memory=pin_memory,
        drop_last=False,
        persistent_workers=min(num_workers, 1) > 0,
    )
    
    logger.info(f"FIXED: CLIP reproduction dataloaders created successfully")
    logger.info(f"  Training dataset length: {len(train_dataset):,}")
    logger.info(f"  Evaluation dataset length: {len(eval_dataset):,}")
    logger.info(f"  Consistent processing: ✅")
    
    return train_dataloader, eval_dataloader