"""Attach structured progress to FastMap without changing its calculations."""
import json
import time


def install_progress_reporting(timer_class, point_pairs_class, write=None, clock=time.monotonic):
    write = write or (lambda value: print(value, flush=True))
    timer_enter, timer_exit = timer_class.__enter__, timer_class.__exit__
    original_query = point_pairs_class.query
    active = []

    def report(**data):
        write('[POINTSTUDIO_STAGE] ' + json.dumps(data))

    def enter_timer(timer):
        # Emit before CUDA synchronization, including the name of a stalled sync.
        report(name=timer.name)
        result = timer_enter(timer)
        active.append(timer.name)
        return result

    def exit_timer(timer, *args):
        try:
            return timer_exit(timer, *args)
        finally:
            active.pop()

    def report_query(pairs, *args, **kwargs):
        reporting = 'Sparse Reconstruction' in active
        completed, reported = 0, -float('inf')
        for batch in original_query(pairs, *args, **kwargs):
            yield batch
            if reporting:
                completed += batch.num_point_pairs
                now = clock()
                if now - reported >= .5 or completed == pairs.num_point_pairs:
                    report(name='Sparse batch', current=completed, total=pairs.num_point_pairs)
                    reported = now

    timer_class.__enter__, timer_class.__exit__ = enter_timer, exit_timer
    point_pairs_class.query = report_query
