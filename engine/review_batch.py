"""Single-owner scheduling for independent reviewers; stage adapters own verdict policy."""

from policy import ProviderFailure


def collect_failure(previous, failure):
    """Retain every trusted observation while preserving the first public pause reason."""
    if previous is None:
        return failure
    previous.observations.extend(failure.observations)
    return previous


def run(adapter):
    """Submit a roster, observe fairly, and apply saved results only on the owner thread."""
    transport = adapter.transport
    live = {}
    paused = None
    try:
        keys = adapter.open()
        initial = [key for key in keys if not adapter.retained(key)]
        prepared = adapter.reserve(initial, initial=True)
        for key, turn in prepared.items():
            transport.check_owner()
            live[key] = (turn, transport.submit(turn))
            adapter.status(key, "queued")
        while adapter.valid():
            transport.check_owner()
            # Retain every result ready in this sweep before applying a terminal judgment.
            failures = []
            for key, (turn, handle) in list(live.items()):
                try:
                    result = transport.observe(handle)
                    adapter.status(key, getattr(handle, "status", "running"))
                    if result is None:
                        continue
                    adapter.complete(key, turn, result, getattr(handle, "timing", None))
                except Exception as error:
                    failures.append((key, handle, error))
                else:
                    del live[key]
            # A failed member must not erase a sibling already ready in this sweep.
            for key, handle, error in failures:
                transport.cancel(handle)
                del live[key]
                try:
                    adapter.failed(key, error)
                except ProviderFailure as failure:
                    paused = collect_failure(paused, failure)
            if paused:
                # Apply saved peer verdicts under existing authority: a terminal verdict
                # still stops the whole batch, while corrections cannot admit new work.
                for key in keys:
                    if not adapter.valid():
                        break
                    if adapter.retained(key):
                        transport.check_owner()
                        adapter.apply(key)
                if not adapter.valid():
                    break
                # Each admitted peer keeps its exact funding/session authority; its own
                # Exchange receipt stops it. A shared provider alone does not revoke it.
                if not live:
                    raise paused
                transport.idle()
                continue
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
                    try:
                        prepared = adapter.reserve([key], initial=False)
                    except ProviderFailure as failure:
                        paused = collect_failure(paused, failure)
                        break
                    if prepared:
                        turn = prepared[key]
                        live[key] = (turn, transport.submit(turn))
                        adapter.status(key, "queued")
            if paused:
                if not live:
                    raise paused
                transport.idle()
                continue
            if live:
                transport.idle()
            elif not adapter.finished():
                raise RuntimeError("review batch has no pending work")
    finally:
        failure = None
        for _key, (_, handle) in live.items():
            try:
                transport.cancel(handle)
            except Exception as error:
                failure = error
        adapter.close()
        if failure is not None:
            raise failure
