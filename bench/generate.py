"""Create one explicit load sweep without changing workload or serving configuration."""

import json
import os
from pathlib import Path
import tempfile

from .matrix import load_matrix, named, plan_matrix
from .provenance import timestamp


def assignments(items, label):
    result = {}
    for item in items:
        name, separator, value = item.partition('=')
        named(name)
        if not separator or not value or name in result:
            raise ValueError(f'{label} needs unique NAME=VALUE entries')
        result[name] = value
    return result


def create_matrix(args):
    requested = Path(args.output).absolute()
    output = requested.parent.resolve() / requested.name
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists; choose a new matrix path')
    sources = assignments(args.stream, '--stream')
    holds = assignments(args.hold, '--hold')
    if args.sweep not in sources or set(holds) != set(sources) - {args.sweep}:
        raise ValueError('Choose one named --sweep and provide --hold NAME=VALUE for every other stream')
    if args.axis == 'rates' and args.max_concurrency is None:
        raise ValueError('Rate sweeps require an explicit --max-concurrency')
    if args.axis == 'concurrency' and (args.max_concurrency is not None or args.arrival is not None):
        raise ValueError('--arrival and --max-concurrency apply only to rate sweeps')
    values = json.loads('[' + args.values + ']')
    if not values:
        raise ValueError('--values needs comma-separated positive load points')
    streams = {}
    for name, source in sources.items():
        load = {args.axis: values if name == args.sweep else [json.loads(holds[name])]}
        if args.axis == 'rates':
            load.update(arrival=args.arrival or 'constant', max_concurrency=args.max_concurrency)
        streams[name] = {'config': os.path.relpath(Path(source).resolve(), output.parent), 'load': load}
    unit = 'requests/s' if args.axis == 'rates' else 'concurrent requests'
    fixed = ', '.join(f'{name}={value}' for name, value in holds.items())
    change = f'Sweep {args.sweep} over {args.values} {unit}.'
    if fixed:
        change += f' Hold {fixed} {unit}.'
    change += ' Keep serving configuration and workload inputs fixed.'
    spec = {'schema_version': 1, 'name': named(args.name), 'repeats': args.repeats,
            'max_attempts_per_repeat': args.max_attempts_per_repeat,
            'min_overlap_seconds': args.min_overlap_seconds,
            'stages': [{'id': args.name, 'question': args.question, 'change': change, 'streams': streams}]}
    # Validate from the destination directory so relative workload paths resolve exactly.
    # Publish atomically without overwriting an existing file, including a racing creator.
    with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, prefix='.matrix-', suffix='.json') as draft:
        json.dump(spec, draft, indent=2)
        draft.write('\n')
        draft.flush()
        config = load_matrix(draft.name)
        plan = plan_matrix(config, output.parent / 'results-preview', 'aiperf')
        os.link(draft.name, output)
    return {'status': 'plan_only', 'matrix': str(output), 'created': timestamp(),
            'rows': plan['rows'], 'repeats_per_row': plan['repeats_per_row'],
            'max_requests_first_pass': plan['max_requests_first_pass'],
            'max_requests_with_manual_retries': plan['max_requests_with_manual_retries'],
            'traffic_sent': False, 'change': change,
            'next': 'Review with make matrix-plan MATRIX=<saved path> RUN=<new results directory>'}
