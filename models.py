"""Definition of the generative models."""

from typing import Callable, Optional, Sequence, Tuple, Union

import e3nn_jax as e3nn
import haiku as hk
import jax
import jax.numpy as jnp
import jraph
import mace_jax.modules
import flax.linen as nn
import chex
import functools

import datatypes

RADII = jnp.arange(0.75, 2.03, 0.02)
NUM_ELEMENTS = 5


def get_first_node_indices(graphs: jraph.GraphsTuple) -> jnp.ndarray:
    """Returns the indices of the focus nodes in each graph."""
    return jnp.concatenate((jnp.asarray([0]), jnp.cumsum(graphs.n_node)[:-1]))


@functools.partial(jax.jit, static_argnames="num_segments")
def segment_sample(probabilities: jnp.ndarray, segment_ids: jnp.ndarray, num_segments: int, rng: chex.PRNGKey):
    """Sample indices from a categorical distribution across each segment.
    Args:
        probabilities: A 1D array of probabilities.
        segment_ids: A 1D array of segment ids.
        num_segments: The number of segments.
        rng: A PRNG key.
    Returns:
        A 1D array of sampled indices.
    """
    def sample_for_segment(rng, i):
        return jax.random.choice(rng, node_indices, p=jnp.where(i == segment_ids, probabilities, 0.))
    
    node_indices = jnp.arange(len(segment_ids))
    rngs = jax.random.split(rng, num_segments)
    return jax.vmap(sample_for_segment)(rngs, jnp.arange(num_segments))


def add_graphs_tuples(
    graphs: jraph.GraphsTuple, other_graphs: jraph.GraphsTuple
) -> jraph.GraphsTuple:
    """Adds the nodes, edges and global features from other_graphs to graphs."""
    return graphs._replace(
        nodes=graphs.nodes + other_graphs.nodes,
        edges=graphs.edges + other_graphs.edges,
        globals=graphs.globals + other_graphs.globals,
    )


class MLP(nn.Module):
    """A multi-layer perceptron."""

    feature_sizes: Sequence[int]
    activation: Callable[[jnp.ndarray], jnp.ndarray] = nn.relu
    layer_norm: bool = True

    @nn.compact
    def __call__(self, inputs: jnp.ndarray) -> jnp.ndarray:
        x = inputs
        for size in self.feature_sizes:
            x = nn.Dense(features=size)(x)
            x = self.activation(x)
            if self.layer_norm:
                x = nn.LayerNorm()(x)
        return x


class S2Activation(nn.Module):
    """Applies a non-linearity after projecting the signal to the sphere."""

    irreps: e3nn.Irreps
    resolution: Union[int, Tuple[int, int]]
    activation: Callable[[jnp.ndarray], jnp.ndarray] = nn.relu
    lmax_out: Optional[int] = None
    layer_norm: bool = True

    @staticmethod
    def _complete_lmax_and_res(
        lmax: Optional[int], res_beta: Optional[int], res_alpha: Optional[int]
    ) -> Tuple[int, int, int]:
        """Fills in the missing values for lmax, res_beta and res_alpha for e3nn.to_s2grid().

        To use FFT accurately, we would want:
            2 * (lmax) + 1 == res_alpha
            2 * (lmax + 1) == res_beta
        """
        if all(arg is None for arg in [lmax, res_beta, res_alpha]):
            raise ValueError("All the entries are None.")

        if res_beta is None:
            if lmax is not None:
                res_beta = 2 * (lmax + 1)  # minimum req. to go on sphere and back
            elif res_alpha is not None:
                res_beta = 2 * ((res_alpha + 1) // 2)

        if res_alpha is None:
            if lmax is not None:
                if res_beta is not None:
                    res_alpha = max(2 * lmax + 1, res_beta - 1)
                else:
                    res_alpha = 2 * lmax + 1  # minimum req. to go on sphere and back
            elif res_beta is not None:
                res_alpha = res_beta - 1

        if lmax is None:
            lmax = min(
                res_beta // 2 - 1, (res_alpha - 1) // 2
            )  # maximum possible to go on sphere and back

        assert res_beta % 2 == 0
        assert lmax + 1 <= res_beta // 2

        return lmax, res_beta, res_alpha

    def _extract_irreps_info(self) -> Tuple[int, int, int, int]:
        """Extracts information about the irreps and resolution of the input and output."""

        irreps = e3nn.Irreps(self.irreps).simplify()
        _, (lmax, _) = irreps[-1]

        assert all(mul == 1 for mul, _ in irreps)
        assert irreps.ls == list(range(lmax + 1))

        # The input transforms as : A_l ---> p_val * (p_arg)^l * A_l
        # The sphere signal transforms as : f(r) ---> p_val * f(p_arg * r)
        if self.lmax_out is None:
            lmax_out = lmax

        try:
            lmax, res_beta, res_alpha = self._complete_lmax_and_res(lmax, *self.res)
        except TypeError:
            lmax, res_beta, res_alpha = self._complete_lmax_and_res(
                lmax, self.res, None
            )

        return lmax, res_beta, res_alpha, lmax_out

    @nn.compact
    def __call__(self, feature_coeffs: e3nn.IrrepsArray) -> e3nn.IrrepsArray:
        (
            lmax,
            res_beta,
            res_alpha,
            lmax_out,
        ) = self._extract_irreps_info()
        assert feature_coeffs.irreps == self.irreps
        features = e3nn.to_s2grid(
            feature_coeffs,
            res_beta,
            res_alpha,
            quadrature="gausslegendre",
        )
        features = features.apply(self.activation)
        if self.layer_norm:
            features = nn.LayerNorm()(features)
        updated_feature_coeffs = e3nn.from_s2grid(
            features,
            lmax_out,
            lmax_in=lmax,
        )
        return updated_feature_coeffs


class S2MLP(nn.Module):
    """A E(3)-equivariant MLP with S2 activations."""

    layers_irreps_out: Sequence[e3nn.Irreps]
    activation: Callable[[jnp.ndarray], jnp.ndarray] = lambda x: x
    skip_connections: bool = False
    s2_grid_resolution: Union[int, Tuple[int, int]] = 100

    @nn.compact
    def __call__(self, inputs: e3nn.IrrepsArray) -> e3nn.IrrepsArray:
        for index, irreps_out in enumerate(self.layers_irreps_out):
            # Apply linear layer.
            next_inputs = e3nn.flax.Linear(irreps_out)(inputs)

            # Apply activation.
            all_irreps = e3nn.Irreps(
                [(1, (l, -1)) for l in range(1 + next_inputs.irreps.lmax)]
            )
            next_inputs = e3nn.flax.Linear(all_irreps)(next_inputs)
            next_inputs = S2Activation(
                next_inputs.irreps, self.activation, self.s2_grid_resolution
            )(next_inputs)

            # Add skip connection.
            if self.skip_connections:
                next_inputs = e3nn.concatenate([next_inputs, inputs])

            inputs = next_inputs
        return inputs


class GraphMLP(nn.Module):
    """Applies an MLP to each node in the graph, with no message-passing."""

    latent_size: int
    num_mlp_layers: int
    position_coeffs_lmax: int
    activation: Callable[[jnp.ndarray], jnp.ndarray] = nn.relu
    layer_norm: bool = True

    @nn.compact
    def __call__(self, graphs: jraph.GraphsTuple) -> datatypes.Predictions:
        species_embedder = nn.Embed(NUM_ELEMENTS, self.latent_size)

        def embed_node_fn(nodes: datatypes.NodesInfo):
            species_embedded = species_embedder(nodes.species)
            positions_embedded = MLP(
                [self.latent_size * self.num_mlp_layers],
                activation=self.activation,
                layer_norm=self.layer_norm,
            )(nodes.positions)
            return nn.Dense(self.latent_size)(
                jnp.concatenate([species_embedded, positions_embedded], axis=-1)
            )

        # Embed the nodes.
        processed_graphs = jraph.GraphMapFeatures(embed_node_fn=embed_node_fn)(graphs)

        # Predict the properties.
        node_embeddings = processed_graphs.nodes
        true_focus_node_embeddings = node_embeddings[get_first_node_indices(graphs)]
        target_species_embeddings = species_embedder(graphs.globals.target_species)

        focus_logits = nn.Dense(1)(node_embeddings).squeeze(axis=-1)
        species_logits = nn.Dense(NUM_ELEMENTS)(true_focus_node_embeddings)

        irreps = e3nn.s2_irreps(self.position_coeffs_lmax, p_val=1, p_arg=-1)
        input_for_position_coeffs = jnp.concatenate(
            (true_focus_node_embeddings, target_species_embeddings), axis=-1
        )
        position_coeffs = nn.Dense(len(RADII) * irreps.dim)(
            input_for_position_coeffs
        )
        position_coeffs = jnp.reshape(position_coeffs, (-1, len(RADII), irreps.dim))
        position_coeffs = e3nn.IrrepsArray(irreps=irreps, array=position_coeffs)

        return datatypes.Predictions(
            focus_logits=focus_logits,
            species_logits=species_logits,
            position_coeffs=position_coeffs,
        )


class GraphNet(nn.Module):
    """A complete Graph Network model defined with Jraph."""

    latent_size: int
    num_mlp_layers: int
    message_passing_steps: int
    position_coeffs_lmax: int
    use_edge_model: bool
    skip_connections: bool = True
    layer_norm: bool = True

    @nn.compact
    def __call__(self, graphs: jraph.GraphsTuple) -> datatypes.Predictions:
        species_embedder = nn.Embed(NUM_ELEMENTS, self.latent_size)

        def embed_node_fn(nodes: datatypes.NodesInfo):
            species_embedded = species_embedder(nodes.species)
            positions_embedded = MLP(
                [self.latent_size * self.num_mlp_layers],
                activation=jax.nn.relu,
                layer_norm=self.layer_norm,
            )(nodes.positions)
            return nn.Dense(self.latent_size)(
                jnp.concatenate([species_embedded, positions_embedded], axis=-1)
            )

        # We will first linearly project the original features as 'embeddings'.
        num_graphs = graphs.n_node.shape[0]
        num_edges = graphs.senders.shape[0]
        embedder = jraph.GraphMapFeatures(
            embed_node_fn=embed_node_fn,
            embed_edge_fn=lambda _: jnp.ones((num_edges, self.latent_size)),
            embed_global_fn=lambda _: jnp.ones((num_graphs, self.latent_size)),
        )
        processed_graphs = embedder(graphs)

        # Now, we will apply a Graph Network once for each message-passing round.
        mlp_feature_sizes = [self.latent_size] * self.num_mlp_layers
        for _ in range(self.message_passing_steps):
            if self.use_edge_model:
                update_edge_fn = jraph.concatenated_args(
                    MLP(
                        mlp_feature_sizes,
                    )
                )
            else:
                update_edge_fn = None

            update_node_fn = jraph.concatenated_args(
                MLP(
                    mlp_feature_sizes,
                )
            )
            update_global_fn = jraph.concatenated_args(
                MLP(
                    mlp_feature_sizes,
                )
            )

            graph_net = jraph.GraphNetwork(
                update_node_fn=update_node_fn,
                update_edge_fn=update_edge_fn,
                update_global_fn=update_global_fn,
            )

            if self.skip_connections:
                processed_graphs = add_graphs_tuples(
                    graph_net(processed_graphs), processed_graphs
                )
            else:
                processed_graphs = graph_net(processed_graphs)

            if self.layer_norm:
                processed_graphs = processed_graphs._replace(
                    nodes=nn.LayerNorm()(processed_graphs.nodes),
                    edges=nn.LayerNorm()(processed_graphs.edges),
                    globals=nn.LayerNorm()(processed_graphs.globals),
                )

        # Predict the properties.
        node_embeddings = processed_graphs.nodes
        true_focus_node_embeddings = node_embeddings[get_first_node_indices(graphs)]
        target_species_embeddings = species_embedder(graphs.globals.target_species)

        focus_logits = nn.Dense(1)(node_embeddings).squeeze(axis=-1)
        species_logits = nn.Dense(NUM_ELEMENTS)(true_focus_node_embeddings)

        irreps = e3nn.s2_irreps(self.position_coeffs_lmax, p_val=1, p_arg=-1)
        input_for_position_coeffs = jnp.concatenate(
            (true_focus_node_embeddings, target_species_embeddings), axis=-1
        )
        position_coeffs = nn.Dense(len(RADII) * irreps.dim)(
            input_for_position_coeffs
        )
        position_coeffs = jnp.reshape(position_coeffs, (-1, len(RADII), irreps.dim))
        position_coeffs = e3nn.IrrepsArray(irreps=irreps, array=position_coeffs)

        return datatypes.Predictions(
            focus_logits=focus_logits,
            species_logits=species_logits,
            position_coeffs=position_coeffs,
        )


# Haiku implementations of the models.
def shifted_softplus(x: jnp.ndarray) -> jnp.ndarray:
    """A softplus function that is shifted so that shifted_softplus(0) = 0."""
    return jax.nn.softplus(x) - jnp.log(2.0)


class GSchNetContinuousFilterConvolution(hk.Module):
    """A continuous filter convolution as defined by GSchNet."""
    def __init__(self, latent_size: int, activation: Callable[[jnp.ndarray], jnp.ndarray], name: Optional[str] = None):
        super().__init__(name)
        self.latent_size = latent_size
        self.activation = activation

    def __call__(self, graphs: datatypes.Fragment) -> jraph.GraphsTuple:
        """Returns the updated graphs after a single interaction block."""
        def embed_distance(distances: jnp.ndarray) -> jnp.ndarray:
            centers = jnp.linspace(0, 10, 100)
            gamma = 1.0
            return jax.vmap(lambda center: jnp.exp(-gamma * jnp.square(distances - center)))(centers)
        
        def compute_embedded_distance(edge_features: jnp.ndarray, sender_features: jnp.ndarray, receiver_features: jnp.ndarray, globals: jnp.ndarray) -> jnp.ndarray:
            """Computes the distance between the two nodes connected by the edge."""
            del edge_features, globals
            distances = jnp.linalg.norm(sender_features - receiver_features)
            distances = embed_distance(distances)
            distances = hk.nets.MLP([self.latent_size, self.latent_size], activation=self.activation)(distances)
            return distances

        return jraph.GraphNetwork(
            update_edge_fn=compute_embedded_distance,
        )(graphs)


class GSchNetInteractionBlock(hk.Module):
    """A single interaction block as defined by GSchNet."""
    def __init__(self, latent_size: int, activation: Callable[[jnp.ndarray], jnp.ndarray], name: Optional[str] = None):
        super().__init__(name)
        self.latent_size = latent_size
        self.activation = activation

    def __call__(self, graphs: jraph.GraphsTuple) -> jraph.GraphsTuple:
        """Returns the updated graphs after a single interaction block."""
        processed_graphs = GSchNetContinuousFilterConvolution(self.latent_size, self.activation)(graphs)
        return jraph.GraphMapFeatures(embed_node_fn=lambda nodes: hk.nets.MLP([self.latent_size, self.latent_size, self.latent_size], activation=self.activation)(nodes))(processed_graphs)



class GSchNet(hk.Module):
    """A Haiku implementation of GSchNet."""

    def __init__(
        self,
        latent_size: int,
        num_interactions: int,
        activation: Callable[[jnp.ndarray], jnp.ndarray] = shifted_softplus,
        name: Optional[str] = None,
    ):
        super().__init__(name=name)
        self.latent_size = latent_size
        self.num_interactions = num_interactions
        self.activation = activation
        self.species_embedder = hk.Embed(NUM_ELEMENTS, self.latent_size)


    def node_embeddings(self, graphs: jraph.GraphsTuple) -> jnp.ndarray:
        """Returns the node embeddings for the given graphs."""
        # Embed the nodes.
        processed_graphs = jraph.GraphMapFeatures(embed_node_fn=lambda nodes: self.species_embedder(nodes.species))(graphs)
        
        # Apply interactions, which involves message-passing over the graph.
        for _ in range(self.num_interactions):
            processed_graphs = GSchNetInteractionBlock(self.latent_size, self.activation)(processed_graphs)

        # Predict the properties.
        return processed_graphs.nodes

    def target_species_logits(self, node_embeddings: jnp.ndarray, n_node: jnp.ndarray) -> jnp.ndarray:
        """Returns the logits for the target species conditioned on all node embeddings."""
        num_nodes = node_embeddings.shape[0]
        num_graphs = n_node.shape[0]

        all_species_embeddings = self.species_embedder(jnp.arange(NUM_ELEMENTS))
        node_and_species_embeddings = jax.vmap(lambda node_embedding: jnp.multiply(node_embedding, all_species_embeddings))(node_embeddings)
        target_species_logits = hk.nets.MLP([self.latent_size, self.latent_size, NUM_ELEMENTS], activation=self.activation)(node_and_species_embeddings)
        
        # Aggregate the target species logits per-graph.
        assert target_species_logits.shape == (num_nodes, NUM_ELEMENTS)
        target_species_logits = e3nn.scatter_sum(target_species_logits, n_node)
        assert target_species_logits.shape == (num_graphs, NUM_ELEMENTS)


    def distance_logits(self, node_embeddings: jnp.ndarray, target_species: jnp.ndarray):
        num_nodes = node_embeddings.shape[0]

        target_species_embeddings = self.species_embedder(target_species)
        node_and_target_species_embeddings = jax.vmap(lambda node_embedding: jnp.multiply(node_embedding, target_species_embeddings))(node_embeddings)
        distance_logits = hk.nets.MLP([self.latent_size, self.latent_size, len(RADII)], activation=self.activation)(node_and_target_species_embeddings)
    
        assert distance_logits.shape == (num_nodes, len(RADII))
        return distance_logits


    def __call__(self, graphs: jraph.GraphsTuple) -> datatypes.Predictions:
        node_embeddings = self.node_embeddings(graphs)
        target_species_logits = self.target_species_logits(node_embeddings, graphs.n_node)
        distance_logits = self.distance_logits(node_embeddings, graphs.target_species)

        return datatypes.TriangulationPredictions(
            target_species_logits=target_species_logits,
            distance_logits=distance_logits,
        )


class MACE(hk.Module):
    """Wrapper class for the Haiku version of MACE."""

    def __init__(
        self,
        output_irreps: str,
        r_max: float,
        num_interactions: int,
        hidden_irreps: str,
        readout_mlp_irreps: str,
        avg_num_neighbors: int,
        num_species: int,
        max_ell: int,
        position_coeffs_lmax: int,
        run_in_evaluation_mode: bool,
        name: Optional[str] = None,
    ):
        super().__init__(name=name)
        self.output_irreps = e3nn.Irreps(output_irreps)
        self.r_max = r_max
        self.num_interactions = num_interactions
        self.hidden_irreps = hidden_irreps
        self.readout_mlp_irreps = readout_mlp_irreps
        self.avg_num_neighbors = avg_num_neighbors
        self.num_species = num_species
        self.max_ell = max_ell
        self.position_coeffs_lmax = position_coeffs_lmax
        self.run_in_evaluation_mode = run_in_evaluation_mode

    def node_embeddings(self, graphs: datatypes.Fragment) -> e3nn.IrrepsArray:
        """Returns node embeddings for these graphs.
        Inputs:
            graphs: a jraph.GraphsTuple with the following fields:
            - nodes.positions
            - nodes.species
            - senders
            - receivers
        
        """
        vectors = (
            graphs.nodes.positions[graphs.receivers]
            - graphs.nodes.positions[graphs.senders]
        )
        species = graphs.nodes.species
        num_nodes = species.shape[0]

        # Predict the properties.
        node_embeddings: e3nn.IrrepsArray = mace_jax.modules.MACE(
            output_irreps=self.output_irreps,
            r_max=self.r_max,
            num_interactions=self.num_interactions,
            hidden_irreps=self.hidden_irreps,
            readout_mlp_irreps=self.readout_mlp_irreps,
            avg_num_neighbors=self.avg_num_neighbors,
            num_species=self.num_species,
            radial_basis=lambda x, x_max: e3nn.bessel(x, 8, x_max),
            radial_envelope=e3nn.soft_envelope,
            max_ell=self.max_ell,
        )(vectors, species, graphs.senders, graphs.receivers)

        assert node_embeddings.shape == (
            num_nodes,
            self.num_interactions,
            self.output_irreps.dim,
        )
        node_embeddings = node_embeddings.axis_to_mul(axis=1)
        return node_embeddings

    def focus_logits(self, node_embeddings: e3nn.IrrepsArray) -> jnp.ndarray:
        """Returns the logits for the focus node."""

        focus_logits = e3nn.haiku.Linear("0e")(node_embeddings)
        focus_logits = focus_logits.array.squeeze(axis=-1)

        num_nodes = node_embeddings.shape[0]
        assert focus_logits.shape == (num_nodes,)
        return focus_logits

    def target_species_logits(self, focus_node_embeddings: e3nn.IrrepsArray) -> jnp.ndarray:
        """Returns the logits for the target species conditioned on only focus node embeddings."""
        num_graphs = focus_node_embeddings.shape[0]

        species_logits = e3nn.haiku.MultiLayerPerceptron(
            list_neurons=[128, NUM_ELEMENTS],
            act=jax.nn.softplus,
        )(focus_node_embeddings.filter(keep="0e")).array
        assert species_logits.shape == (num_graphs, NUM_ELEMENTS)

        return species_logits

    def target_position(
        self, focus_node_embeddings: e3nn.IrrepsArray, target_species: jnp.ndarray
    ) -> e3nn.IrrepsArray:
        """Returns the position coefficients for the target species."""
        num_graphs = focus_node_embeddings.shape[0]
        assert focus_node_embeddings.shape == (
            num_graphs,
            focus_node_embeddings.irreps.dim,
        )

        irreps = e3nn.s2_irreps(self.position_coeffs_lmax, p_val=1, p_arg=-1)
        target_species_embeddings = hk.Embed(
            NUM_ELEMENTS, focus_node_embeddings.irreps.num_irreps
        )(target_species)

        assert target_species_embeddings.shape == (
            num_graphs,
            focus_node_embeddings.irreps.num_irreps,
        )

        position_coeffs = e3nn.haiku.Linear(len(RADII) * irreps)(
            target_species_embeddings * focus_node_embeddings
        )
        position_coeffs = position_coeffs.mul_to_axis(factor=len(RADII))

        num_radii = len(RADII)
        assert position_coeffs.shape == (
            num_graphs,
            num_radii,
            irreps.dim,
        )

        return position_coeffs

    def get_training_predictions(
        self,  graphs: datatypes.Fragment
    ) -> datatypes.Predictions:
        """Returns the predictions on these graphs during training, when we have access to the true focus and target species."""
        # Get the node embeddings.
        node_embeddings = self.node_embeddings(graphs)

        # Get the focus logits.
        focus_logits = self.focus_logits(node_embeddings)

        # Get the embeddings of the focus nodes.
        # These are the first nodes in each graph during training.
        focus_node_indices = get_first_node_indices(graphs)
        true_focus_node_embeddings = node_embeddings[focus_node_indices]

        # Get the species logits.
        target_species_logits = self.target_species_logits(true_focus_node_embeddings)

        # Get the position coefficients.
        position_coeffs = self.target_position(
            true_focus_node_embeddings, graphs.globals.target_species
        )
        return datatypes.Predictions(
            focus_logits=focus_logits,
            target_species_logits=target_species_logits,
            position_coeffs=position_coeffs,
        )

    def get_evaluation_predictions(
        self, graphs: datatypes.Fragment
    ) -> datatypes.EvaluationPredictions:
        """Returns the predictions on a single padded graph during evaluation, when we do not have access to the true focus and target species."""
        # Get the PRNG key.
        rng = hk.next_rng_key()

        # Get the number of graphs and nodes.
        num_nodes = graphs.nodes.positions.shape[0]
        num_graphs = graphs.n_node.shape[0]

        # Get the node embeddings, and corresponding focus probabilities.
        node_embeddings = self.node_embeddings(graphs)
        focus_logits = self.focus_logits(node_embeddings)
        focus_probs = jraph.partition_softmax(focus_logits, graphs.n_node, num_nodes)

        # Sample the focus node.
        rng, focus_rng = jax.random.split(rng)
        segment_ids = jnp.repeat(
            jnp.arange(num_graphs),
            graphs.n_node,
            axis=0,
            total_repeat_length=num_nodes)
        focus_indices = segment_sample(focus_probs, segment_ids, num_graphs, focus_rng)

        # Get the embeddings of the focus node.
        focus_node_embeddings = node_embeddings[focus_indices]

        # Get the species logits.
        target_species_logits = self.target_species_logits(focus_node_embeddings)
        target_species_probs = jax.nn.softmax(target_species_logits)

        # Sample the target species.
        rng, species_rng = jax.random.split(rng)
        species_rngs = jax.random.split(species_rng, num_graphs)
        target_species = jax.vmap(lambda key, p: jax.random.choice(
            key, NUM_ELEMENTS, p=p))(species_rngs, target_species_probs)

        # Get the position coefficients.
        position_coeffs = self.target_position(
            focus_node_embeddings, target_species
        )

        return datatypes.EvaluationPredictions(
            focus_logits=focus_logits,
            focus_indices=focus_indices,
            target_species_logits=target_species_logits,
            position_coeffs=position_coeffs,
            target_species=target_species,
        )

    def __call__(self, graphs: datatypes.Fragment) -> datatypes.Predictions:
        if self.run_in_evaluation_mode:
            return self.get_evaluation_predictions(graphs)
        return self.get_training_predictions(graphs)
