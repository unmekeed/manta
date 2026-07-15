"""Точка входа: python -m extractor [--backfill-all | --match ID]."""
import argparse
import logging

from .runner import Extractor, ExtractorConfig

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
)

parser = argparse.ArgumentParser()
parser.add_argument("--backfill-all", action="store_true",
                    help="пересчитать фичи всех матчей витрины и выйти")
parser.add_argument("--match", type=int, default=0,
                    help="пересчитать фичи одного матча и выйти")
args = parser.parse_args()

ex = Extractor(ExtractorConfig())
if args.backfill_all:
    ex.backfill()
elif args.match:
    ex.backfill([args.match])
else:
    ex.run()
