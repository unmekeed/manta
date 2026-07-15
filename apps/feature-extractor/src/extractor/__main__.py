"""Точка входа: python -m extractor."""
import logging

from .runner import Extractor, ExtractorConfig

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
)

Extractor(ExtractorConfig()).run()
