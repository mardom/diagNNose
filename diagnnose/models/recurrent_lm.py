import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import torch
from overrides import overrides
from torch import Tensor, nn

from diagnnose.activations.selection_funcs import final_token
from diagnnose.corpus import import_corpus
from diagnnose.extractors.base_extractor import Extractor
from diagnnose.typedefs import config as config
from diagnnose.typedefs.activations import ActivationDict, ActivationNames
from diagnnose.corpus import Corpus
from diagnnose.utils import __file__ as diagnnose_utils_init
from diagnnose.utils.misc import suppress_print
from diagnnose.utils.pickle import load_pickle

# layer -> name (hx/cx) -> size
SizeDict = Dict[int, Dict[str, int]]


class RecurrentLM(ABC, nn.Module):
    """ Abstract class for LM with intermediate activations """

    device: str = "cpu"
    forget_offset: int = 0
    ih_concat_order: List[str] = ["h", "i"]
    sizes: SizeDict = {}
    split_order: List[str]
    use_char_embs: bool = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.init_states: ActivationDict = {}

    @property
    def num_layers(self) -> int:
        return len(self.sizes)

    @property
    def top_layer(self) -> int:
        return self.num_layers - 1

    @property
    def output_size(self) -> int:
        return self.sizes[self.top_layer]["h"]

    @overrides
    @abstractmethod
    def forward(
        self, input_: Tensor, prev_activations: ActivationDict, compute_out: bool = True
    ) -> Tuple[Optional[Tensor], ActivationDict]:
        """ Performs a single forward pass across all rnn layers.

        Parameters
        ----------
        input_ : Tensor
            Tensor containing a batch of token id's at the current
            sentence position.
        prev_activations : TensorDict, optional
            Dict mapping the activation names of the previous hidden
            and cell states to their corresponding Tensors. Defaults to
            None, indicating the initial states will be used.
        compute_out : bool, optional
            Toggles the computation of the final decoder projection.
            If set to False this projection is not calculated.
            Defaults to True.

        Returns
        -------
        out : torch.Tensor, optional
            Torch Tensor of output distribution of vocabulary. If
            `compute_out` is set to True, `out` returns None.
        activations : TensorDict
            Dictionary mapping each layer to each activation name to a
            tensor.
        """

    def init_hidden(self, batch_size: int) -> ActivationDict:
        """Creates a batch of initial states.

        Parameters
        ----------
        batch_size : int
            Size of batch for which states are created.

        Returns
        -------
        init_states : ActivationTensors
            Dictionary mapping hidden and cell state to init tensors.
        """
        init_states = self._expand_batch_size(self.init_states, batch_size)

        for k, v in init_states.items():
            init_states[k] = v.to(self.device)

        return init_states

    def final_hidden(self, hidden: ActivationDict) -> Tensor:
        """ Returns the final hidden state.

        Parameters
        ----------
        hidden : ActivationTensors
            Dictionary of extracted activations.

        Returns
        -------
        final_hidden : Tensor
            Tensor of the final hidden state.
        """
        return hidden[self.top_layer, "hx"].squeeze()

    def set_init_states(
        self,
        pickle_path: Optional[str] = None,
        corpus_path: Optional[str] = None,
        use_default: bool = False,
        save_init_states_to: Optional[str] = None,
        vocab_path: Optional[str] = None,
    ) -> None:
        """ Set up the initial LM states.

        If no path is provided 0-valued embeddings will be used.
        Note that the loaded init should provide tensors for `hx`
        and `cx` in all layers of the LM.

        Note that `init_states_pickle` takes precedence over
        `init_states_corpus` in case both are provided.

        Parameters
        ----------
        pickle_path : str, optional
            Path to pickled file with initial lstm states. If not
            provided zero-valued init states will be created.
        corpus_path : str, optional
            Path to corpus of which the final hidden state will be used
            as initial states.
        save_init_states_to : str, optional
            Path to which the newly computed init_states will be saved.
            If not provided these states won't be dumped.
        vocab_path : str, optional
            Path to the model vocabulary, which should a file containing
            a vocab entry at each line. Must be provided when creating
            the init states from a corpus.

        Returns
        -------
        init_states : ActivationTensors
            ActivationTensors containing the init states for each layer.
        """
        if use_default:
            diagnnose_utils_dir = os.path.dirname(diagnnose_utils_init)
            corpus_path = os.path.join(diagnnose_utils_dir, "init_sentence.txt")

        if pickle_path is not None:
            print("Loading extracted init states from file")
            init_states: ActivationDict = load_pickle(pickle_path)
            self._validate(init_states)
        elif corpus_path is not None:
            assert (
                vocab_path is not None
            ), "Vocab path must be provided when creating init states from corpus"
            print("Creating init states from provided corpus")
            init_states = self._create_init_states_from_corpus(
                corpus_path, vocab_path, save_init_states_to
            )
        else:
            init_states = self.create_zero_states()

        self.init_states = init_states

    def create_zero_states(self, batch_size: int = 1) -> ActivationDict:
        """Zero-initialized states if no init state is provided.

        Parameters
        ----------
        batch_size : int, optional
            Batch size should be provided if it's larger than 1.

        Returns
        -------
        init_states : ActivationTensors
            Dictionary mapping (layer, name) tuple to zero-tensor.
        """
        init_states: ActivationDict = {}

        for layer in range(self.num_layers):
            init_states[layer, "cx"] = self.create_zero_state(batch_size, layer, "c")
            init_states[layer, "hx"] = self.create_zero_state(batch_size, layer, "h")

        return init_states

    def create_zero_state(self, batch_size: int, layer: int, cell_type: str) -> Tensor:
        """ Create single zero tensor for given layer/cell_type.

        Parameters
        ----------
        batch_size : int
            Batch size for model task.
        layer : int
            Model layer.
        cell_type : str
            Either `h` or `c`.

        Returns
        -------
        tensor : Tensor
            Zero-valued tensor of the correct size.
        """
        return torch.zeros(
            (batch_size, self.sizes[layer][cell_type]), dtype=config.DTYPE
        )

    @suppress_print
    def _create_init_states_from_corpus(
        self,
        init_states_corpus: str,
        vocab_path: str,
        save_init_states_to: Optional[str],
    ) -> ActivationDict:
        corpus: Corpus = import_corpus(init_states_corpus, vocab_path=vocab_path)

        activation_names: ActivationNames = [
            (layer, name) for layer in range(self.num_layers) for name in ["hx", "cx"]
        ]

        self.init_states = self.create_zero_states()
        extractor = Extractor(
            self, corpus, activation_names, activations_dir=save_init_states_to
        )
        init_states = extractor.extract(
            dynamic_dumping=False, selection_func=final_token
        )

        return init_states

    def _validate(self, init_states: ActivationDict) -> None:
        """ Performs a simple validation of the new initial states.

        Parameters
        ----------
        init_states: ActivationTensors
            New initial states that should have a structure that
            complies with the dimensions of the language model.
        """
        # Multiplied by 2 because there are hx & cx for each layer
        assert (
            len(init_states) == self.num_layers * 2
        ), "Number of initial layers not correct"

        for layer, layer_size in self.sizes.items():
            for hc in ["h", "c"]:
                assert (
                    layer,
                    f"{hc}x",
                ) in init_states.keys(), (
                    f"Activation {layer},{hc}x is not found in init states"
                )

                init_size = init_states[layer, f"{hc}x"].size(1)
                model_size = self.sizes[layer][hc]
                assert init_size == model_size, (
                    f"Initial activation size for {hc}x is incorrect: "
                    f"{hc}x: {init_size}, should be {model_size}"
                )

    def _expand_batch_size(
        self, init_states: ActivationDict, batch_size: int
    ) -> ActivationDict:
        """Expands the init_states in the batch dimension."""
        batch_init_states: ActivationDict = {}

        for layer in range(self.num_layers):
            for hc in ["hx", "cx"]:
                # Shape: (batch_size, nhid)
                batch_init_states[layer, hc] = init_states[layer, hc].repeat(
                    batch_size, 1
                )

        return batch_init_states