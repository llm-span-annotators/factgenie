"""
The Propaganda Techniques Corpus (PTC) is a corpus of propagandistic techniques at annotated by 6 pro-annotators in spans.
    The corpus includes 451 articles (350k tokens) from 48 news outlets. 
    The corpus accompanied paper [Fine-Grained Analysis of Propaganda in News Article](https://aclanthology.org/D19-1565/).
    The labels are:
Loaded Language, Name Calling&Labeling, Repetition, Exaggeration&Minimization, Doubt, Appeal to fear-prejudice, Flag-Waving, Causal Oversimplification, Slogans, Appeal to Authority, Black-and-White Fallacy, Thought-terminating Cliches, Whataboutism, Reductio ad Hitlerum, Red Herring, Bandwagon, Obfuscation&Intentional&Vagueness&Confusion, Straw Men
"""

import logging
from factgenie.datasets.basic import JSONLDataset

logger = logging.getLogger("factgenie")

class PropagandaTechniques(JSONLDataset):
    def render(self, example):
        return None