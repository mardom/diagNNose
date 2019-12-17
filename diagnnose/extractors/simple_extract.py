import shutil

from diagnnose.extractors.base_extractor import Extractor
from diagnnose.models.lm import LanguageModel
from diagnnose.typedefs.activations import ActivationNames, RemoveCallback, SelectFunc
from diagnnose.typedefs.corpus import Corpus
from diagnnose.utils.misc import suppress_print

BATCH_SIZE = 1024


# @suppress_print
def simple_extract(
    model: LanguageModel,
    activations_dir: str,
    corpus: Corpus,
    activation_names: ActivationNames,
    selection_func: SelectFunc = lambda sen_id, pos, item: True,
) -> RemoveCallback:
    """ Basic extraction method.

    Returns
    -------
    remove_activations : RemoveCallback
        callback function that can be executed at the end of a procedure
        that depends on the extracted activations. Removes all the
        activations that have been extracted. Takes no arguments.
    """
    extractor = Extractor(model, corpus, activations_dir, activation_names)

    extractor.extract(
        batch_size=BATCH_SIZE, dynamic_dumping=True, selection_func=selection_func
    )

    def remove_activations():
        shutil.rmtree(activations_dir)

    return remove_activations