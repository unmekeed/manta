"""Точка входа: python -m reportgen [--match ID] (без --match — Kafka-петля)."""
import argparse
import logging

from .runner import ReportGenerator, ReportgenConfig

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s",'
           '"service":"report-generator","msg":"%(message)s"}')

parser = argparse.ArgumentParser()
parser.add_argument("--match", type=int, default=0,
                    help="сгенерировать отчёт для одного матча и выйти "
                         "(бэкфилл/отладка)")
args = parser.parse_args()

gen = ReportGenerator(ReportgenConfig())
if args.match:
    print(gen.generate(args.match))
else:
    gen.run()
