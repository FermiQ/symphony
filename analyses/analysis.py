"""Loads the model from a workdir to perform analysis."""

import glob
import os
import pickle
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

import haiku as hk
import ase
import ase.build
import e3nn_jax as e3nn
import jax
import jax.numpy as jnp
import jraph
import ml_collections
import numpy as np
import pandas as pd
import tensorflow as tf
import yaml
from absl import logging
from clu import checkpoint
from flax.training import train_state
import plotly.graph_objects as go
import plotly.subplots

# from openbabel import pybel
# from openbabel import openbabel as ob

sys.path.append("..")

import qm9
import datatypes
import input_pipeline
import models
import train
from configs import root_dirs

try:
    import input_pipeline_tf

    tf.config.experimental.set_visible_devices([], "GPU")
except ImportError:
    logging.warning("TensorFlow not installed. Skipping import of input_pipeline_tf.")
    pass


ATOMIC_NUMBERS = models.ATOMIC_NUMBERS
ELEMENTS = ["H", "C", "N", "O", "F"]
RADII = models.RADII
NUMBER_TO_SYMBOL = {1: "H", 6: "C", 7: "N", 8: "O", 9: "F"}

# Colors and sizes for the atoms.
ATOMIC_COLORS = {
    1: "rgb(150, 150, 150)",  # H
    6: "rgb(50, 50, 50)",  # C
    7: "rgb(0, 100, 255)",  # N
    8: "rgb(255, 0, 0)",  # O
    9: "rgb(255, 0, 255)",  # F
}
ATOMIC_SIZES = {
    1: 10,  # H
    6: 30,  # C
    7: 30,  # N
    8: 30,  # O
    9: 30,  # F
}


def get_title_for_name(name: str) -> str:
    """Returns the title for the given name."""
    if "e3schnet" in name:
        return "E3SchNet"
    elif "mace" in name:
        return "MACE"
    elif "nequip" in name:
        return "NequIP"
    return name.title()


def combine_visualizations(
    figs: Sequence[go.Figure],
    label_name: Optional[str] = None,
    labels: Optional[Sequence[str]] = None,
) -> go.Figure:
    """Combines multiple plotly figures generated by visualize_predictions() into one figure with a slider."""
    all_traces = []
    for fig in figs:
        all_traces.extend(fig.data)

    # Save the original visibility of the traces.
    original_visibility = {i: trace.visible for i, trace in enumerate(all_traces)}

    steps = []
    ct = 0
    start_indices = [0]
    for fig in figs:
        steps.append(
            dict(
                method="restyle",
                args=[
                    {
                        "visible": [
                            original_visibility[i]
                            if ct <= i < ct + len(fig.data)
                            else False
                            for i, trace in enumerate(all_traces)
                        ]
                    }
                ],
            )
        )
        ct += len(fig.data)
        start_indices.append(ct)

    if label_name is None:
        label_name = "steps"
    if labels is None:
        labels = steps

    axis = dict(
        showbackground=False,
        showticklabels=False,
        showgrid=False,
        zeroline=False,
        title="",
        nticks=3,
    )
    layout = dict(
        sliders=[{label_name: labels}],
        title_x=0.5,
        width=1500,
        height=800,
        scene=dict(
            xaxis=dict(**axis),
            yaxis=dict(**axis),
            zaxis=dict(**axis),
            aspectmode="data",
        ),
        paper_bgcolor="rgba(255,255,255,1)",
        plot_bgcolor="rgba(255,255,255,1)",
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="right",
            x=0.1,
        ),
    )

    fig_all = plotly.subplots.make_subplots(
        rows=1,
        cols=4,
        specs=[[{"type": "scene"}, {"type": "scene"}, {"type": "xy"}, {"type": "xy"}]],
        subplot_titles=(
            "Input Fragment",
            "Predictions",
            "Focus and Atom Type Probabilities",
            "Stop Probability",
        ),
    )

    for i, trace in enumerate(all_traces):
        visible = original_visibility[i] if i < start_indices[1] else False
        trace.update(visible=visible)
        if trace.type in ["scatter3d", "surface"]:
            col = 1
        else:
            col = 2
        fig_all.add_trace(trace, row=1, col=col)
    fig_all.update_layout(layout)
    return fig_all


def get_plotly_traces_for_fragment(
    fragment: datatypes.Fragments,
) -> Sequence[go.Scatter3d]:
    """Returns the plotly traces for the fragment."""
    atomic_numbers = list(
        int(num) for num in models.get_atomic_numbers(fragment.nodes.species)
    )
    molecule_traces = []
    molecule_traces.append(
        go.Scatter3d(
            x=fragment.nodes.positions[:, 0],
            y=fragment.nodes.positions[:, 1],
            z=fragment.nodes.positions[:, 2],
            mode="markers",
            marker=dict(
                size=[ATOMIC_SIZES[num] for num in atomic_numbers],
                color=[ATOMIC_COLORS[num] for num in atomic_numbers],
            ),
            hovertext=[
                f"Element: {ase.data.chemical_symbols[num]}" for num in atomic_numbers
            ],
            opacity=1.0,
            name="Molecule Atoms",
            legendrank=1,
        )
    )
    # Add bonds.
    for i, j in zip(fragment.senders, fragment.receivers):
        molecule_traces.append(
            go.Scatter3d(
                x=fragment.nodes.positions[[i, j], 0],
                y=fragment.nodes.positions[[i, j], 1],
                z=fragment.nodes.positions[[i, j], 2],
                line=dict(color="black"),
                mode="lines",
                showlegend=False,
            )
        )

    # Highlight the target atom.
    if fragment.globals is not None:
        if fragment.globals.target_positions is not None and not fragment.globals.stop:
            # The target position is relative to the fragment's focus node.
            target_positions = fragment.globals.target_positions + fragment.nodes.positions[0]
            target_positions = target_positions.reshape(3)
            molecule_traces.append(
                go.Scatter3d(
                    x=[target_positions[0]],
                    y=[target_positions[1]],
                    z=[target_positions[2]],
                    mode="markers",
                    marker=dict(
                        size=[
                            1.05
                            * ATOMIC_SIZES[
                                models.ATOMIC_NUMBERS[
                                    fragment.globals.target_species.item()
                                ]
                            ]
                        ],
                        color=["green"],
                    ),
                    opacity=0.5,
                    name="Target Atom",
                )
            )

    return molecule_traces


def get_plotly_traces_for_predictions(
    pred: datatypes.Predictions, fragment: datatypes.Fragments
) -> Sequence[go.Scatter3d]:
    """Returns a list of plotly traces for the prediction."""

    atomic_numbers = list(
        int(num) for num in models.get_atomic_numbers(fragment.nodes.species)
    )
    focus = pred.globals.focus_indices.item()
    focus_position = fragment.nodes.positions[focus]
    focus_and_target_species_probs = pred.nodes.focus_and_target_species_probs
    focus_probs = focus_and_target_species_probs.sum(axis=-1)
    num_nodes, num_elements = focus_and_target_species_probs.shape

    # Highlight the focus probabilities, obtained by marginalization over all elements.
    def get_scaling_factor(focus_prob: float, num_nodes: int) -> float:
        """Returns a scaling factor for the size of the atom."""
        if focus_prob < 1 / num_nodes - 1e-3:
            return 0.95
        return 1 + focus_prob**2

    def chosen_focus_string(index: int, focus: int) -> str:
        """Returns a string indicating whether the atom was chosen as the focus."""
        if index == focus:
            return f"Atom {index} (Chosen as Focus)"
        return f"Atom {index} (Not Chosen as Focus)"

    molecule_traces = []
    molecule_traces.append(
        go.Scatter3d(
            x=fragment.nodes.positions[:, 0],
            y=fragment.nodes.positions[:, 1],
            z=fragment.nodes.positions[:, 2],
            mode="markers",
            marker=dict(
                size=[
                    get_scaling_factor(float(focus_prob), num_nodes) * ATOMIC_SIZES[num]
                    for focus_prob, num in zip(focus_probs, atomic_numbers)
                ],
                color=["rgba(150, 75, 0, 0.5)" for _ in range(num_nodes)],
            ),
            hovertext=[
                f"Focus Probability: {focus_prob:.3f}<br>{chosen_focus_string(i, focus)}"
                for i, focus_prob in enumerate(focus_probs)
            ],
            name="Focus Probabilities",
        )
    )

    # Highlight predicted position, if not stopped.
    if not pred.globals.stop:
        predicted_target_position = focus_position + pred.globals.position_vectors
        molecule_traces.append(
            go.Scatter3d(
                x=[predicted_target_position[0]],
                y=[predicted_target_position[1]],
                z=[predicted_target_position[2]],
                mode="markers",
                marker=dict(
                    size=[
                        1.05
                        * ATOMIC_SIZES[
                            models.ATOMIC_NUMBERS[pred.globals.target_species.item()]
                        ]
                    ],
                    color=["purple"],
                ),
                opacity=0.5,
                name="Predicted Atom",
            )
        )

    # Since we downsample the position grid, we need to recompute the position probabilities.
    position_coeffs = pred.globals.position_coeffs
    position_logits = models.log_coeffs_to_logits(
        position_coeffs, 50, 99
    )
    position_logits.grid_values -= jnp.max(position_logits.grid_values)
    position_probs = position_logits.apply(jnp.exp)

    count = 0
    cmin = 0.0
    cmax = position_probs.grid_values.max().item()
    for i in range(len(RADII)):
        prob_r = position_probs[i]

        # Skip if the probability is too small.
        if prob_r.grid_values.max() < 1e-2 * cmax:
            continue

        count += 1
        surface_r = go.Surface(
            **prob_r.plotly_surface(radius=RADII[i], translation=focus_position),
            colorscale=[[0, "rgba(4, 59, 192, 0.)"], [1, "rgba(4, 59, 192, 1.)"]],
            showscale=False,
            cmin=cmin,
            cmax=cmax,
            name="Position Probabilities",
            legendgroup="Position Probabilities",
            showlegend=(count == 1),
            visible="legendonly",
        )
        molecule_traces.append(surface_r)

    # Plot spherical harmonic projections of logits.
    # Find closest index in RADII to the sampled positions.
    radius = jnp.linalg.norm(pred.globals.position_vectors, axis=-1)
    most_likely_radius_index = jnp.abs(RADII - radius).argmin()
    most_likely_radius = RADII[most_likely_radius_index]
    all_sigs = e3nn.to_s2grid(
        position_coeffs, 50, 99, quadrature="soft", p_val=1, p_arg=-1
    )
    cmin = all_sigs.grid_values.min().item()
    cmax = all_sigs.grid_values.max().item()
    for channel in range(position_coeffs.shape[0]):
        most_likely_radius_coeffs = position_coeffs[channel, most_likely_radius_index]
        most_likely_radius_sig = e3nn.to_s2grid(
            most_likely_radius_coeffs, 50, 99, quadrature="soft", p_val=1, p_arg=-1
        )
        spherical_harmonics = go.Surface(
            most_likely_radius_sig.plotly_surface(
                scale_radius_by_amplitude=True,
                radius=most_likely_radius,
                translation=focus_position,
                normalize_radius_by_max_amplitude=True,
            ),
            cmin=cmin,
            cmax=cmax,
            name=f"Spherical Harmonics for Logits: Channel {channel}",
            showlegend=True,
            visible="legendonly",
        )
        molecule_traces.append(spherical_harmonics)

    # Plot target species probabilities.
    stop_probability = pred.globals.stop_probs.item()
    predicted_target_species = pred.globals.target_species.item()
    if fragment.globals is not None and not fragment.globals.stop:
        true_focus = 0  # This is a convention used in our training pipeline.
        true_target_species = fragment.globals.target_species.item()
    else:
        true_focus = None
        true_target_species = None

    # We highlight the true target if provided.
    def get_focus_string(atom_index: int) -> str:
        """Get the string for the focus."""
        base_string = f"Atom {atom_index}"
        if atom_index == focus:
            base_string = f"{base_string}<br>Predicted Focus"
        if atom_index == true_focus:
            base_string = f"{base_string}<br>True Focus"
        return base_string

    def get_atom_type_string(element_index: int, element: str) -> str:
        """Get the string for the atom type."""
        base_string = f"{element}"
        if element_index == predicted_target_species:
            base_string = f"{base_string}<br>Predicted Species"
        if element_index == true_target_species:
            base_string = f"{base_string}<br>True Species"
        return base_string

    focus_and_atom_type_traces = [
        go.Heatmap(
            x=[
                get_atom_type_string(index, elem)
                for index, elem in enumerate(ELEMENTS[:num_elements])
            ],
            y=[get_focus_string(i) for i in range(num_nodes)],
            z=np.round(pred.nodes.focus_and_target_species_probs, 3),
            texttemplate="%{z}",
            showlegend=False,
            showscale=False,
            colorscale="Blues",
            zmin=0.0,
            zmax=1.0,
            xgap=1,
            ygap=1,
        ),
    ]
    stop_traces = [
        go.Bar(
            x=["STOP"],
            y=[stop_probability],
            showlegend=False,
        )
    ]
    return molecule_traces, focus_and_atom_type_traces, stop_traces


def visualize_fragment(
    fragment: datatypes.Fragments,
) -> go.Figure:
    """Visualizes the predictions for a molecule at a particular step."""
    # Make subplots.
    fig = plotly.subplots.make_subplots(
        rows=1,
        cols=1,
        specs=[[{"type": "scene"}]],
        subplot_titles=("Input Fragment",),
    )

    # Traces corresponding to the input fragment.
    fragment_traces = get_plotly_traces_for_fragment(fragment)
    for trace in fragment_traces:
        fig.add_trace(trace, row=1, col=1)

    # Update the layout.
    axis = dict(
        showbackground=False,
        showticklabels=False,
        showgrid=False,
        zeroline=False,
        title="",
        nticks=3,
    )
    fig.update_layout(
        scene=dict(
            xaxis=dict(**axis),
            yaxis=dict(**axis, scaleanchor="x", scaleratio=1),
            zaxis=dict(**axis, scaleanchor="x", scaleratio=1),
            aspectmode="data",
        ),
        paper_bgcolor="rgba(255,255,255,1)",
        plot_bgcolor="rgba(255,255,255,1)",
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="right",
            x=0.1,
        ),
    )

    try:
        return go.FigureWidget(fig)
    except NotImplementedError:
        return fig


def visualize_predictions(
    pred: datatypes.Predictions,
    fragment: datatypes.Fragments,
) -> go.Figure:
    """Visualizes the predictions for a molecule at a particular step."""

    # Make subplots.
    fig = plotly.subplots.make_subplots(
        rows=1,
        cols=4,
        specs=[[{"type": "scene"}, {"type": "scene"}, {"type": "xy"}, {"type": "xy"}]],
        column_widths=[0.4, 0.4, 0.1, 0.1],
        subplot_titles=("Input Fragment", "Predictions", "", ""),
    )

    # Traces corresponding to the input fragment.
    fragment_traces = get_plotly_traces_for_fragment(fragment)

    # Traces corresponding to the prediction.
    (
        predicted_fragment_traces,
        focus_and_atom_type_traces,
        stop_traces,
    ) = get_plotly_traces_for_predictions(pred, fragment)

    for trace in fragment_traces:
        fig.add_trace(trace, row=1, col=1)
        trace.showlegend = False
        fig.add_trace(trace, row=1, col=2)

    for trace in predicted_fragment_traces:
        fig.add_trace(trace, row=1, col=2)

    for trace in focus_and_atom_type_traces:
        fig.add_trace(trace, row=1, col=3)

    for trace in stop_traces:
        fig.add_trace(trace, row=1, col=4)

    # Update the layout.
    centre_of_mass = jnp.mean(fragment.nodes.positions, axis=0)
    furthest_dist = jnp.max(
        jnp.linalg.norm(
            fragment.nodes.positions + pred.globals.position_vectors - centre_of_mass,
            axis=-1,
        )
    )
    furthest_dist = jnp.max(
        jnp.linalg.norm(
            fragment.nodes.positions - pred.globals.position_vectors - centre_of_mass,
            axis=-1,
        )
    )
    min_range = centre_of_mass - furthest_dist
    max_range = centre_of_mass + furthest_dist
    axis = dict(
        showbackground=False,
        showticklabels=False,
        showgrid=False,
        zeroline=False,
        title="",
        nticks=3,
    )
    fig.update_layout(
        scene1=dict(
            xaxis=dict(**axis, range=[min_range[0], max_range[0]]),
            yaxis=dict(**axis, range=[min_range[1], max_range[1]]),
            zaxis=dict(**axis, range=[min_range[2], max_range[2]]),
            aspectmode="manual",
            aspectratio=dict(x=1, y=1, z=1),
        ),
        scene2=dict(
            xaxis=dict(**axis, range=[min_range[0], max_range[0]]),
            yaxis=dict(**axis, range=[min_range[1], max_range[1]]),
            zaxis=dict(**axis, range=[min_range[2], max_range[2]]),
            aspectmode="manual",
            aspectratio=dict(x=1, y=1, z=1),
        ),
        yaxis2=dict(
            range=[0, 1],
        ),
        paper_bgcolor="rgba(255,255,255,1)",
        plot_bgcolor="rgba(255,255,255,1)",
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="right",
            x=0.1,
        ),
    )

    # Sync cameras.
    try:
        fig_widget = go.FigureWidget(fig)

        def cam_change_1(layout, camera):
            fig_widget.layout.scene2.camera = camera

        def cam_change_2(layout, camera):
            if fig_widget.layout.scene1.camera != camera:
                fig_widget.layout.scene1.camera = camera

        fig_widget.layout.scene1.on_change(cam_change_1, "camera")
        fig_widget.layout.scene2.on_change(cam_change_2, "camera")

        return fig_widget
    except NotImplementedError:
        return fig


def cast_keys_as_int(dictionary: Dict[Any, Any]) -> Dict[Any, Any]:
    """Returns a dictionary with string keys converted to integers, wherever possible."""
    casted_dictionary = {}
    for key, val in dictionary.items():
        try:
            val = cast_keys_as_int(val)
        except AttributeError:
            pass

        try:
            key = int(key)
        except ValueError:
            pass
        finally:
            casted_dictionary[key] = val
    return casted_dictionary


def name_from_workdir(workdir: str) -> str:
    """Returns the full name of the model from the workdir."""
    index = workdir.find("workdirs") + len("workdirs/")
    return workdir[index:]


def config_to_dataframe(config: ml_collections.ConfigDict) -> Dict[str, Any]:
    """Flattens a nested config into a Pandas dataframe."""

    # Compatibility with old configs.
    if "num_interactions" not in config:
        config.num_interactions = config.n_interactions
        del config.n_interactions

    if "num_channels" not in config:
        config.num_channels = config.n_atom_basis
        assert config.num_channels == config.n_filters
        del config.n_atom_basis, config.n_filters

    def iterate_with_prefix(dictionary: Dict[str, Any], prefix: str):
        """Iterates through a nested dictionary, yielding the flattened and prefixed keys and values."""
        for k, v in dictionary.items():
            if isinstance(v, dict):
                yield from iterate_with_prefix(v, prefix=f"{prefix}{k}.")
            else:
                yield prefix + k, v

    config_dict = dict(iterate_with_prefix(config.to_dict(), "config."))
    return pd.DataFrame().from_dict([config_dict])


def load_model_at_step(
    workdir: str, step: int, run_in_evaluation_mode: bool
) -> Tuple[ml_collections.ConfigDict, hk.Transformed, Dict[str, jnp.ndarray]]:
    """Loads the model at a given step.

    This is a lightweight version of load_from_workdir, that only constructs the model and not the training state.
    """

    if step == -1:
        params_file = os.path.join(workdir, "checkpoints/params_best.pkl")
    else:
        params_file = os.path.join(workdir, f"checkpoints/params_{step}.pkl")

    try:
        with open(params_file, "rb") as f:
            params = pickle.load(f)
    except FileNotFoundError:
        if step == -1:
            try:
                params_file = os.path.join(workdir, "checkpoints/params.pkl")
                with open(params_file, "rb") as f:
                    params = pickle.load(f)
            except:
                raise FileNotFoundError(f"Could not find params file {params_file}")
        else:
            raise FileNotFoundError(f"Could not find params file {params_file}")

    with open(workdir + "/config.yml", "rt") as config_file:
        config = yaml.unsafe_load(config_file)
    assert config is not None
    config = ml_collections.ConfigDict(config)
    config.root_dir = root_dirs.get_root_dir(
        config.dataset, config.get("fragment_logic", "nn")
    )

    model = models.create_model(config, run_in_evaluation_mode=run_in_evaluation_mode)
    params = jax.tree_map(jnp.asarray, params)
    return model, params, config


def get_results_as_dataframe(
    basedir: str
) -> pd.DataFrame:
    """Returns the results for the given model as a pandas dataframe for each split."""

    results = pd.DataFrame()
    for config_file_path in glob.glob(
        os.path.join(basedir, "**", "*.yml"), recursive=True
    ):
        workdir = os.path.dirname(config_file_path)
        try:
            config, best_state, _, metrics_for_best_state = load_from_workdir(
                workdir
            )
        except FileNotFoundError:
            logging.warning(f"Skipping {workdir} because it is incomplete.")
            continue

        num_params = sum(
            jax.tree_util.tree_leaves(jax.tree_map(jnp.size, best_state.params))
        )
        config_df = config_to_dataframe(config)
        other_df = pd.DataFrame.from_dict(
            {
                "model": [config.model.lower()],
                "max_l": [config.max_ell],
                "num_interactions": [config.num_interactions],
                "num_channels": [config.num_channels],
                "num_params": [num_params],
                # "num_train_molecules": [
                #     config.train_molecules[1] - config.train_molecules[0]
                # ],
            }
        )
        df = pd.merge(config_df, other_df, left_index=True, right_index=True)
        for split in metrics_for_best_state:
            metrics_for_split = {
                f"{split}.{metric}": [metrics_for_best_state[split][metric].item()]
                for metric in metrics_for_best_state[split]
            }
            metrics_df = pd.DataFrame.from_dict(metrics_for_split)
            df = pd.merge(df, metrics_df, left_index=True, right_index=True)
        results = pd.concat([results, df], ignore_index=True)

    return results


def load_metrics_from_workdir(
    workdir: str,
) -> Tuple[
    ml_collections.ConfigDict,
    train_state.TrainState,
    train_state.TrainState,
    Dict[Any, Any],
]:
    """Loads only the config and the metrics for the best model."""

    if not os.path.exists(workdir):
        raise FileNotFoundError(f"{workdir} does not exist.")

    # Load config.
    saved_config_path = os.path.join(workdir, "config.yml")
    if not os.path.exists(saved_config_path):
        raise FileNotFoundError(f"No saved config found at {workdir}")

    logging.info("Saved config found at %s", saved_config_path)
    with open(saved_config_path, "r") as config_file:
        config = yaml.unsafe_load(config_file)

    # Check that the config was loaded correctly.
    assert config is not None
    config = ml_collections.ConfigDict(config)
    config.root_dir = root_dirs.get_root_dir(
        config.dataset, config.get("fragment_logic", "nn")
    )

    checkpoint_dir = os.path.join(workdir, "checkpoints")
    ckpt = checkpoint.Checkpoint(checkpoint_dir, max_to_keep=5)
    data = ckpt.restore({"metrics_for_best_state": None})

    return config, cast_keys_as_int(data["metrics_for_best_state"])


def load_from_workdir(
    workdir: str,
    load_pickled_params: bool = True,
    init_graphs: Optional[jraph.GraphsTuple] = None,
) -> Tuple[
    ml_collections.ConfigDict,
    train_state.TrainState,
    train_state.TrainState,
    Dict[Any, Any],
]:
    """Loads the config, best model (in train mode), best model (in eval mode) and metrics for the best model."""

    if not os.path.exists(workdir):
        raise FileNotFoundError(f"{workdir} does not exist.")

    # Load config.
    saved_config_path = os.path.join(workdir, "config.yml")
    if not os.path.exists(saved_config_path):
        raise FileNotFoundError(f"No saved config found at {workdir}")

    logging.info("Saved config found at %s", saved_config_path)
    with open(saved_config_path, "r") as config_file:
        config = yaml.unsafe_load(config_file)

    # Check that the config was loaded correctly.
    assert config is not None
    config = ml_collections.ConfigDict(config)
    config.root_dir = root_dirs.get_root_dir(
        config.dataset, config.get("fragment_logic", "nn")
    )

    # Mimic what we do in train.py.
    rng = jax.random.PRNGKey(config.rng_seed)
    rng, dataset_rng = jax.random.split(rng)

    # Set up dummy variables to obtain the structure.
    rng, init_rng = jax.random.split(rng)

    net = models.create_model(config, run_in_evaluation_mode=False)
    eval_net = models.create_model(config, run_in_evaluation_mode=True)

    # If we have pickled parameters already, we don't need init_graphs to initialize the model.
    # Note that we restore the model parameters from the checkpoint anyways.
    # We only use the pickled parameters to initialize the model, so only the keys of the pickled parameters are important.
    if load_pickled_params:
        checkpoint_dir = os.path.join(workdir, "checkpoints")
        pickled_params_file = os.path.join(checkpoint_dir, "params_best.pkl")
        if not os.path.exists(pickled_params_file):
            pickled_params_file = os.path.join(checkpoint_dir, "params_best.pkl")
            if not os.path.exists(pickled_params_file):
                raise FileNotFoundError(
                    f"No pickled params found at {pickled_params_file}"
                )

        logging.info(
            "Initializing dummy model with pickled params found at %s",
            pickled_params_file,
        )

        with open(pickled_params_file, "rb") as f:
            params = jax.tree_map(np.array, pickle.load(f))
    else:
        if init_graphs is None:
            logging.info("Initializing dummy model with init_graphs from dataloader")
            datasets = input_pipeline_tf.get_datasets(dataset_rng, config)
            train_iter = datasets["train"].as_numpy_iterator()
            init_graphs = next(train_iter)
        else:
            logging.info("Initializing dummy model with provided init_graphs")

        params = jax.jit(net.init)(init_rng, init_graphs)

    tx = train.create_optimizer(config)
    dummy_state = train_state.TrainState.create(
        apply_fn=net.apply, params=params, tx=tx
    )

    # Load the actual values.
    checkpoint_dir = os.path.join(workdir, "checkpoints")
    ckpt = checkpoint.Checkpoint(checkpoint_dir, max_to_keep=5)
    data = ckpt.restore({"best_state": dummy_state, "metrics_for_best_state": None})
    best_state = jax.tree_map(jnp.asarray, data["best_state"])
    best_state_in_eval_mode = best_state.replace(apply_fn=eval_net.apply)

    return (
        config,
        best_state,
        best_state_in_eval_mode,
        cast_keys_as_int(data["metrics_for_best_state"]),
    )


def construct_molecule(molecule_str: str) -> Tuple[ase.Atoms, str]:
    """Returns a molecule from the given input string.

    The input is interpreted either as an index for the QM9 dataset,
    a name for ase.build.molecule(),
    or a file with atomic numbers and coordinates for ase.io.read().
    """
    # If we believe the string is a file, try to read it.
    if os.path.exists(molecule_str):
        filename = os.path.basename(molecule_str).split(".")[0]
        return ase.io.read(molecule_str), filename

    # A number is interpreted as a QM9 molecule index.
    if molecule_str.isdigit():
        dataset = qm9.load_qm9("qm9_data")
        molecule = dataset[int(molecule_str)]
        return molecule, f"qm9_index={molecule_str}"

    # If the string is a valid molecule name, try to build it.
    molecule = ase.build.molecule(molecule_str)
    return molecule, molecule.get_chemical_formula()


# def construct_obmol(mol: ase.Atoms) -> ob.OBMol:
#     obmol = ob.OBMol()
#     obmol.BeginModify()

#     # set positions and atomic numbers of all atoms in the molecule
#     for p, n in zip(mol.positions, mol.numbers):
#         obatom = obmol.NewAtom()
#         obatom.SetAtomicNum(int(n))
#         obatom.SetVector(*p.tolist())

#     # infer bonds and bond order
#     obmol.ConnectTheDots()
#     obmol.PerceiveBondOrders()

#     obmol.EndModify()
#     return obmol


# def construct_pybel_mol(mol: ase.Atoms) -> pybel.Molecule:
#     """Constructs a Pybel molecule from an ASE molecule."""
#     obmol = construct_obmol(mol)

#     return pybel.Molecule(obmol)
