import os
from typing import Any, Dict, List, Optional

import torch
from torch.nn.utils.rnn import pack_padded_sequence
from tqdm import tqdm

from diagnnose.activations.activation_reader import ActivationReader
from diagnnose.corpus.create_iterator import create_iterator
from diagnnose.corpus.import_corpus import import_corpus
from diagnnose.models.lm import LanguageModel


def linzen_downstream(
    model: LanguageModel,
    vocab_path: str,
    path: str,
    task_activations: Optional[Dict[str, str]] = None,
    tasks: Optional[List[str]] = None,
    device: str = "cpu",
    print_results: bool = True,
) -> float:
    correct = 0.0
    corpus = import_corpus(path, header_from_first_line=True, vocab_path=vocab_path)
    iterator = create_iterator(corpus, batch_size=200, device=device, sort=True)

    for batch in tqdm(iterator):
        sens, slens = batch.sen
        batch_size = batch.batch_size
        packed_sens = pack_padded_sequence(sens, lengths=slens, batch_first=True)

        hidden = model.init_hidden(batch_size)
        final_hidden = torch.zeros(
            (batch_size, model.output_size), dtype=torch.float32
        ).to(device)
        n = 0
        for i, j in enumerate(packed_sens.batch_sizes):
            w = packed_sens[0][n : n + j]
            for name, v in hidden.items():
                hidden[name] = v[:j]
            if model.use_char_embs:
                w = [corpus.examples[batch.idx[k]].sen[i] for k in range(j)]
            _, hidden = model(w, hidden, compute_out=False)
            for k in range(int(j)):
                final_hidden[k] = hidden[model.top_layer, "hx"][k]
            n += j

        output_classes = torch.tensor(
            [
                [
                    corpus.vocab.stoi[batch.verb[i]],
                    corpus.vocab.stoi[batch.wrong_verb[i]],
                ]
                for i in range(batch_size)
            ]
        ).to(torch.long)
        probs = torch.bmm(model.decoder_w[output_classes], final_hidden.unsqueeze(2))
        probs = probs[:, :, 0]
        probs += model.decoder_b[output_classes]

        correct += int(torch.sum(probs[:, 0] > probs[:, 1]))

    accuracy = correct / len(corpus.examples)

    if print_results:
        print(accuracy)

    return accuracy