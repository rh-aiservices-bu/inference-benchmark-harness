"""Create an operator-editable config through the same validator used by runs."""

import argparse
import json
import os
import shlex
from pathlib import Path
import tempfile

from .config import validate


def add_parser(sub):
    parser = sub.add_parser('configure', help='Write a workload config without sending traffic',
                            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--output', default='benchmark.local.json', help='New config file; never overwrites')
    parser.add_argument('--url', required=True, help='Existing inference server base URL')
    parser.add_argument('--model', required=True, help='Served model name')
    parser.add_argument('--path', default='/v1/chat/completions', help='Streaming chat API path')
    parser.add_argument('--no-model-list', action='store_true', help='Skip /v1/models; smoke must confirm inference access')
    parser.add_argument('--api-key-env', help='Environment variable containing the bearer token')
    parser.add_argument('--header', action='append', default=[], metavar='NAME=VALUE', help='Non-secret request header; repeat as needed')
    workload = parser.add_mutually_exclusive_group()
    workload.add_argument('--prompts', help='Local single-turn JSONL with a text field per row')
    workload.add_argument('--input-tokens', type=int, default=128, help='Generated input token length')
    parser.add_argument('--output-tokens', type=int, default=64, help='Requested output token limit')
    parser.add_argument('--tokenizer', default='builtin', help='Tokenizer; choose an approved matching local tokenizer for exact lengths')
    axis = parser.add_mutually_exclusive_group()
    axis.add_argument('--concurrency', nargs='+', type=int, help='Closed-loop load points; omitted load uses 1 2 4')
    axis.add_argument('--rates', nargs='+', type=float, help='Independent request-rate points, requests/s')
    parser.add_argument('--arrival', choices=('constant', 'poisson'), help='Rate-mode arrivals; defaults to constant')
    parser.add_argument('--max-concurrency', type=int, help='Required in rate mode: in-flight request cap')
    for flag, default, help_text in (
        ('requests', 20, 'Request limit per repeat'),
        ('duration-seconds', 60, 'Sending duration limit per repeat'),
        ('request-timeout-seconds', 30, 'Timeout for each request'),
        ('grace-seconds', 35, 'Time allowed for outstanding responses after sending stops'),
        ('deadline-seconds', 180, 'Runner wall-clock limit for the AIPerf process, including startup/export'),
        ('repeats', 3, 'Measurements per load point'),
        ('max-attempts-per-repeat', 3, 'Total attempt allowance per repeat; recovery is manual'),
    ):
        parser.add_argument('--' + flag, type=int, default=default, help=help_text)
    parser.add_argument('--metrics-file', help='JSON array of producer URLs and required metric names; omitted means no server collection')


def create_config(args):
    requested = Path(args.output).absolute()
    output = requested.parent.resolve() / requested.name
    if str(args.output).endswith(os.sep) or output.is_dir():
        raise ValueError("--output must name a file, not a directory")
    if not output.parent.is_dir():
        raise ValueError("Create the parent directory before using --output")
    headers = {}
    for item in args.header:
        name, separator, value = item.partition('=')
        if not separator or name.lower() in {key.lower() for key in headers}:
            raise ValueError('Headers need unique NAME=VALUE entries')
        headers[name] = value
    endpoint = {'url': args.url, 'model': args.model, 'path': args.path}
    if not args.no_model_list:
        endpoint['models_path'] = '/v1/models'
    if args.api_key_env:
        endpoint['api_key_env'] = args.api_key_env
    if headers:
        endpoint['headers'] = headers
    workload = {'type': 'single_turn' if args.prompts else 'synthetic',
                'output_tokens': args.output_tokens, 'tokenizer': args.tokenizer}
    if args.prompts:
        workload['path'] = os.path.relpath(Path(args.prompts).resolve(), output.parent)
    else:
        workload['input_tokens'] = args.input_tokens
    bounds = {key: getattr(args, key) for key in ('requests', 'duration_seconds', 'request_timeout_seconds',
               'grace_seconds', 'deadline_seconds', 'repeats', 'max_attempts_per_repeat')}
    if args.rates is not None:
        if args.max_concurrency is None:
            raise ValueError("--max-concurrency is required with --rates")
        bounds.update(rates=args.rates, arrival=args.arrival or 'constant', max_concurrency=args.max_concurrency)
    else:
        if args.arrival is not None or args.max_concurrency is not None:
            raise ValueError('--arrival and --max-concurrency apply only with --rates')
        bounds['concurrency'] = args.concurrency or [1, 2, 4]
    metrics = json.loads(Path(args.metrics_file).read_text()) if args.metrics_file else []
    if not isinstance(metrics, list) or any(not isinstance(item, dict) for item in metrics):
        raise ValueError('--metrics-file must contain a JSON array of producer objects')
    config = {'schema_version': 1, 'endpoint': endpoint, 'workload': workload,
              'load': bounds, 'metrics': metrics, 'goals': {}}
    validate(json.loads(json.dumps(config)), output.parent)
    # Private temporary file; linking publishes atomically and cannot replace an existing config.
    with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, prefix='.config-', suffix='.json') as draft:
        json.dump(config, draft, indent=2)
        draft.write('\n')
        draft.flush()
        os.fsync(draft.fileno())
        try:
            os.link(draft.name, output)
        except FileExistsError:
            raise ValueError('Config already exists; edit it or choose another --output') from None
    return {'status': 'configured', 'config': str(output), 'traffic_sent': False,
            'next': 'Review the saved settings, then run make verify CONFIG=' + shlex.quote(str(output))}
