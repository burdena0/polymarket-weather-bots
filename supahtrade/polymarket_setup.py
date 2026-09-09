"""Explicit local initialization and hidden-input US account enrollment."""
import argparse
from getpass import getpass
import json
from pathlib import Path
from .polymarket_us_connection import USConnection
from .weather_execution import Execution
from .combined_weather import StrategyExecution


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--enroll',action='store_true');group.add_argument('--initialize',action='store_true')
    a=p.parse_args();project=Path(__file__).resolve().parents[1];root=project/'data/us-weather-combined-v1'
    if a.enroll:
        key=getpass('Polymarket US key ID (hidden): ');secret=getpass('Secret (hidden): ')
        print(json.dumps(USConnection(project/'data/connections').verify(key,secret)))
    else:
        if root.exists() or any((project/'data').glob('us-weather-*/orders.sqlite')):
            raise ValueError('Existing journals detected. Use migration; never reset accounting.')
        root.mkdir(parents=True);(root/'STOP').touch()
        engine=Execution(root/'orders.sqlite',None)
        try:StrategyExecution(engine,'independent',root)
        finally:engine.close()
        (root/'combined.json').write_text(json.dumps({'version':1,'live':True,'initialization':'fresh_account_journal'}))
        print('Initialized with STOP, $10 cumulative budget, no orders enabled. Enroll separately.')

if __name__=='__main__':main()
