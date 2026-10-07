"""Run the current isolated parallel questionnaire benchmark."""
import argparse
import asyncio
from career_sim_runner.constants import REPO_ROOT
from . import DEFAULT_SEED
from .questionnaires import run


def _parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--solution',default=str(REPO_ROOT/'solution'))
    parser.add_argument('--skill-id',default='observe-decide-review')
    parser.add_argument('--seed',default=DEFAULT_SEED)
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--resume-run')
    parser.add_argument('--baseline-run')
    parser.add_argument('--continue-transport',action='store_true',help='After analyst inspection, retain saved answers across a recorded format-only transport revision')
    parser.add_argument('--pilot',action='store_true',help='Pause after the first saved event for prompt/usage inspection')
    return parser


def main():
    args=_parser().parse_args()
    if args.limit < 0:raise SystemExit('--limit must be nonnegative (0 = full pool)')
    print(asyncio.run(run(args)))
