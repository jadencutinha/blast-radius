"""pytest plugin: run ProcessPoolExecutor work in the test's own process.

sqlglot's optimizer and executor tests do their real work in spawned workers,
where per-test tracing cannot see it. Used only while building the maps.
Arguments and results still pass through pickle, as they would between
processes, so a task cannot mutate the caller's objects.
"""

import concurrent.futures
import pickle


class InProcessPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, function, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            call = pickle.loads(pickle.dumps((function, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(call[0](*call[1], **call[2]))))
        except BaseException as error:  # a future carries whatever the task raised
            future.set_exception(error)
        return future

    def map(self, function, *iterables, **_):
        return [self.submit(function, *args).result() for args in zip(*iterables)]

    def shutdown(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


# Plugins load before test modules, whose "from concurrent.futures import" then
# picks this up.
concurrent.futures.ProcessPoolExecutor = InProcessPool
