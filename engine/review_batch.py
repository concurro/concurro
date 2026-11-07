"""Single-owner scheduling for independent reviewers; stage adapters own verdict policy."""


def run(adapter):
    """Submit a roster, observe fairly, and apply saved results only on the owner thread."""
    transport = adapter.transport
    live = {}
    try:
        keys = adapter.open()
        initial = [key for key in keys if not adapter.retained(key)]
        prepared = adapter.reserve(initial, initial=True)
        for key, turn in prepared.items():
            transport.check_owner()
            live[key] = (turn, transport.submit(turn))
            adapter.status(key, 'queued')
        while adapter.valid():
            transport.check_owner()
            # Retain every result ready in this sweep before applying a terminal judgment.
            failures = []
            for key, (turn, handle) in list(live.items()):
                try:
                    result = transport.observe(handle)
                    adapter.status(key, getattr(handle, 'status', 'running'))
                    if result is None:
                        continue
                    adapter.complete(key, turn, result, getattr(handle, 'timing', None))
                except Exception as error:
                    failures.append((key, handle, error))
                else:
                    del live[key]
            # A failed member must not erase a sibling already ready in this sweep.
            for key, handle, error in failures:
                transport.cancel(handle)
                del live[key]
                adapter.failed(key, error)
            for key in keys:
                if not adapter.valid():
                    break
                if adapter.retained(key):
                    transport.check_owner()
                    adapter.apply(key)
            if not adapter.valid() or adapter.finished():
                break
            for key in keys:
                if key not in live and adapter.needs_attempt(key):
                    prepared = adapter.reserve([key], initial=False)
                    if prepared:
                        turn = prepared[key]
                        live[key] = (turn, transport.submit(turn))
                        adapter.status(key, 'queued')
            if live:
                transport.idle()
            elif not adapter.finished():
                raise RuntimeError('review batch has no pending work')
    finally:
        failure = None
        for key, (_, handle) in live.items():
            try:
                transport.cancel(handle)
            except Exception as error:
                failure = error
        adapter.close()
        if failure is not None:
            raise failure
