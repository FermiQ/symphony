"""Defines the default training configuration."""

from typing import Optional
import ml_collections
import os


def get_root_dir() -> Optional[str]:
    """Get the root directory for the QM9 dataset."""
    hostname, username = os.uname()[1], os.environ.get("USER")
    if hostname == "potato.mit.edu":
        return "/home/ameyad/qm9_data_tf/data_tf2"
    elif username == "ameyad":
        return "/Users/ameyad/Documents/qm9_data_tf/data_tf2"
    elif username == "songk":
        return (
            "/home/songk/atomicarchitects/spherical_harmonic_net/qm9_data_tf/data_tf2"
        )
    return None


def get_config() -> ml_collections.ConfigDict:
    """Get the default training configuration."""
    config = ml_collections.ConfigDict()

    config.root_dir = get_root_dir()
    config.rng_seed = 0
    config.train_molecules = (0, 47616)
    config.val_molecules = (47616, 53568)
    config.test_molecules = (53568, 133920)

    config.num_train_steps = 20_000
    config.num_eval_steps = 100
    config.num_eval_steps_at_end_of_training = 20000
    config.log_every_steps = 1000
    config.eval_every_steps = 1000
    config.checkpoint_every_steps = 1000
    config.nn_tolerance = 0.5
    config.nn_cutoff = 5.0
    config.max_n_nodes = 512
    config.max_n_edges = 1024
    config.max_n_graphs = 64
    config.loss_kwargs = {
        "radius_rbf_variance": 1e-3,
    }

    # Prediction heads.
    config.focus_predictor = ml_collections.ConfigDict()
    config.focus_predictor.latent_size = 128
    config.focus_predictor.num_layers = 2

    config.target_species_predictor = ml_collections.ConfigDict()
    config.target_species_predictor.latent_size = 128
    config.target_species_predictor.num_layers = 2

    config.target_position_predictor = ml_collections.ConfigDict()
    config.target_position_predictor.res_beta = 30
    config.target_position_predictor.res_alpha = 51

    return config
