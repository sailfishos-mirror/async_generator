import sys
from functools import wraps
from types import coroutine
import inspect
from inspect import (
    getcoroutinestate, CORO_CREATED, CORO_CLOSED, CORO_SUSPENDED
)
import collections.abc


class YieldWrapper:
    def __init__(self, payload):
        self.payload = payload


def _wrap(value):
    return YieldWrapper(value)


def _is_wrapped(box):
    return isinstance(box, YieldWrapper)


def _unwrap(box):
    return box.payload


# This is the magic code that lets you use yield_ and yield_from_ with native
# generators.
#
# The old version worked great on Linux and MacOS, but not on Windows, because
# it depended on _PyAsyncGenValueWrapperNew. The new version segfaults
# everywhere, and I'm not sure why -- probably my lack of understanding
# of ctypes and refcounts.
#
# There are also some commented out tests that should be re-enabled if this is
# fixed:
#
# if sys.version_info >= (3, 6):
#     # Use the same box type that the interpreter uses internally. This allows
#     # yield_ and (more importantly!) yield_from_ to work in built-in
#     # generators.
#     import ctypes  # mua ha ha.
#
#     # We used to call _PyAsyncGenValueWrapperNew to create and set up new
#     # wrapper objects, but that symbol isn't available on Windows:
#     #
#     #   https://github.com/python-trio/async_generator/issues/5
#     #
#     # Fortunately, the type object is available, but it means we have to do
#     # this the hard way.
#
#     # We don't actually need to access this, but we need to make a ctypes
#     # structure so we can call addressof.
#     class _ctypes_PyTypeObject(ctypes.Structure):
#         pass
#     _PyAsyncGenWrappedValue_Type_ptr = ctypes.addressof(
#         _ctypes_PyTypeObject.in_dll(
#             ctypes.pythonapi, "_PyAsyncGenWrappedValue_Type"))
#     _PyObject_GC_New = ctypes.pythonapi._PyObject_GC_New
#     _PyObject_GC_New.restype = ctypes.py_object
#     _PyObject_GC_New.argtypes = (ctypes.c_void_p,)
#
#     _Py_IncRef = ctypes.pythonapi.Py_IncRef
#     _Py_IncRef.restype = None
#     _Py_IncRef.argtypes = (ctypes.py_object,)
#
#     class _ctypes_PyAsyncGenWrappedValue(ctypes.Structure):
#         _fields_ = [
#             ('PyObject_HEAD', ctypes.c_byte * object().__sizeof__()),
#             ('agw_val', ctypes.py_object),
#         ]
#     def _wrap(value):
#         box = _PyObject_GC_New(_PyAsyncGenWrappedValue_Type_ptr)
#         raw = ctypes.cast(ctypes.c_void_p(id(box)),
#                           ctypes.POINTER(_ctypes_PyAsyncGenWrappedValue))
#         raw.contents.agw_val = value
#         _Py_IncRef(value)
#         return box
#
#     def _unwrap(box):
#         assert _is_wrapped(box)
#         raw = ctypes.cast(ctypes.c_void_p(id(box)),
#                           ctypes.POINTER(_ctypes_PyAsyncGenWrappedValue))
#         value = raw.contents.agw_val
#         _Py_IncRef(value)
#         return value
#
#     _PyAsyncGenWrappedValue_Type = type(_wrap(1))
#     def _is_wrapped(box):
#         return isinstance(box, _PyAsyncGenWrappedValue_Type)


# The magic @coroutine decorator is how you write the bottom level of
# coroutine stacks -- 'async def' can only use 'await' = yield from; but
# eventually we must bottom out in a @coroutine that calls plain 'yield'.
@coroutine
def _yield_(value):
    return (yield _wrap(value))


# But we wrap the bare @coroutine version in an async def, because async def
# has the magic feature that users can get warnings messages if they forget to
# use 'await'.
async def yield_(value=None):
    return await _yield_(value)


async def yield_from_(delegate):
    # Transcribed with adaptations from:
    #
    #   https://www.python.org/dev/peps/pep-0380/#formal-semantics
    #
    # This takes advantage of a sneaky trick: if an @async_generator-wrapped
    # function calls another async function (like yield_from_), and that
    # second async function calls yield_, then because of the hack we use to
    # implement yield_, the yield_ will actually propagate through yield_from_
    # back to the @async_generator wrapper. So even though we're a regular
    # function, we can directly yield values out of the calling async
    # generator.
    def unpack_StopAsyncIteration(e):
        if e.args:
            return e.args[0]
        else:
            return None

    _i = type(delegate).__aiter__(delegate)
    if hasattr(_i, "__await__"):
        _i = await _i
    try:
        _y = await type(_i).__anext__(_i)
    except StopAsyncIteration as _e:
        _r = unpack_StopAsyncIteration(_e)
    else:
        while 1:
            try:
                _s = await yield_(_y)
            except GeneratorExit as _e:
                try:
                    _m = _i.aclose
                except AttributeError:
                    pass
                else:
                    await _m()
                raise _e
            except BaseException as _e:
                _x = sys.exc_info()
                try:
                    _m = _i.athrow
                except AttributeError:
                    raise _e
                else:
                    try:
                        _y = await _m(*_x)
                    except StopAsyncIteration as _e:
                        _r = unpack_StopAsyncIteration(_e)
                        break
            else:
                try:
                    if _s is None:
                        _y = await type(_i).__anext__(_i)
                    else:
                        _y = await _i.asend(_s)
                except StopAsyncIteration as _e:
                    _r = unpack_StopAsyncIteration(_e)
                    break
    return _r


# This is the awaitable / iterator that implements asynciter.__anext__() and
# friends.
#
# Note: we can be sloppy about the distinction between
#
#   type(self._it).__next__(self._it)
#
# and
#
#   self._it.__next__()
#
# because we happen to know that self._it is not a general iterator object,
# but specifically a coroutine iterator object where these are equivalent.
class ANextIter:
    def __init__(self, it, first_fn, *first_args):
        self._it = it
        self._first_fn = first_fn
        self._first_args = first_args

    def __await__(self):
        return self

    def __next__(self):
        if self._first_fn is not None:
            first_fn = self._first_fn
            first_args = self._first_args
            self._first_fn = self._first_args = None
            return self._invoke(first_fn, *first_args)
        else:
            return self._invoke(self._it.__next__)

    def send(self, value):
        return self._invoke(self._it.send, value)

    def throw(self, type, value=None, traceback=None):
        return self._invoke(self._it.throw, type, value, traceback)

    def _invoke(self, fn, *args):
        try:
            result = fn(*args)
        except StopIteration as e:
            # The underlying generator returned, so we should signal the end
            # of iteration.
            raise StopAsyncIteration(e.value)
        except StopAsyncIteration as e:
            # PEP 479 says: if a generator raises Stop(Async)Iteration, then
            # it should be wrapped into a RuntimeError. Python automatically
            # enforces this for StopIteration; for StopAsyncIteration we need
            # to it ourselves.
            raise RuntimeError(
                "async_generator raise StopAsyncIteration"
            ) from e
        if _is_wrapped(result):
            raise StopIteration(_unwrap(result))
        else:
            return result


UNSPECIFIED = object()
try:
    from sys import get_asyncgen_hooks, set_asyncgen_hooks

except ImportError:
    import threading

    asyncgen_hooks = collections.namedtuple(
        "asyncgen_hooks", ("firstiter", "finalizer")
    )

    class _hooks_storage(threading.local):
        def __init__(self):
            self.firstiter = None
            self.finalizer = None

    _hooks = _hooks_storage()

    def get_asyncgen_hooks():
        return asyncgen_hooks(
            firstiter=_hooks.firstiter, finalizer=_hooks.finalizer
        )

    def set_asyncgen_hooks(firstiter=UNSPECIFIED, finalizer=UNSPECIFIED):
        if firstiter is not UNSPECIFIED:
            if firstiter is None or callable(firstiter):
                _hooks.firstiter = firstiter
            else:
                raise TypeError(
                    "callable firstiter expected, got {}".format(
                        type(firstiter).__name__
                    )
                )

        if finalizer is not UNSPECIFIED:
            if finalizer is None or callable(finalizer):
                _hooks.finalizer = finalizer
            else:
                raise TypeError(
                    "callable finalizer expected, got {}".format(
                        type(finalizer).__name__
                    )
                )


class AsyncGenerator:
    def __init__(self, coroutine):
        self._coroutine = coroutine
        self._it = coroutine.__await__()
        self.ag_running = False
        self._finalizer = None
        self._closed = False
        self._hooks_inited = False

    # On python 3.5.0 and 3.5.1, __aiter__ must be awaitable.
    # Starting in 3.5.2, it should not be awaitable, and if it is, then it
    #   raises a PendingDeprecationWarning.
    # See:
    #   https://www.python.org/dev/peps/pep-0492/#api-design-and-implementation-revisions
    #   https://docs.python.org/3/reference/datamodel.html#async-iterators
    #   https://bugs.python.org/issue27243
    if sys.version_info < (3, 5, 2):

        async def __aiter__(self):
            return self
    else:

        def __aiter__(self):
            return self

    ################################################################
    # Introspection attributes
    ################################################################

    @property
    def ag_code(self):
        return self._coroutine.cr_code

    @property
    def ag_frame(self):
        return self._coroutine.cr_frame

    ################################################################
    # Core functionality
    ################################################################

    # These need to return awaitables, rather than being async functions,
    # to match the native behavior where the firstiter hook is called
    # immediately on asend()/etc, even if the coroutine that asend()
    # produces isn't awaited for a bit.

    def __anext__(self):
        return self._do_it(self._it.__next__)

    def asend(self, value):
        return self._do_it(self._it.send, value)

    def athrow(self, type, value=None, traceback=None):
        return self._do_it(self._it.throw, type, value, traceback)

    def _do_it(self, start_fn, *args):
        if not self._hooks_inited:
            self._hooks_inited = True
            (firstiter, self._finalizer) = get_asyncgen_hooks()
            if firstiter is not None:
                firstiter(self)

        # On CPython 3.5.2 (but not 3.5.0), coroutines get cranky if you try
        # to iterate them after they're exhausted. Generators OTOH just raise
        # StopIteration. We want to convert the one into the other, so we need
        # to avoid iterating stopped coroutines.
        if getcoroutinestate(self._coroutine) is CORO_CLOSED:
            raise StopAsyncIteration()

        async def step():
            if self.ag_running:
                raise ValueError("async generator already executing")
            try:
                self.ag_running = True
                return await ANextIter(self._it, start_fn, *args)
            finally:
                self.ag_running = False

        return step()

    ################################################################
    # Cleanup
    ################################################################

    async def aclose(self):
        state = getcoroutinestate(self._coroutine)
        if state is CORO_CLOSED or self._closed:
            return
        # Make sure that even if we raise "async_generator ignored
        # GeneratorExit", and thus fail to exhaust the coroutine,
        # __del__ doesn't complain again.
        self._closed = True
        if state is CORO_CREATED:
            # Make sure that aclose() on an unstarted generator returns
            # successfully and prevents future iteration.
            self._it.close()
            return
        try:
            await self.athrow(GeneratorExit)
        except (GeneratorExit, StopAsyncIteration):
            pass
        else:
            raise RuntimeError("async_generator ignored GeneratorExit")

    def __del__(self):
        if getcoroutinestate(self._coroutine) is CORO_CREATED:
            # Never started, nothing to clean up, just suppress the "coroutine
            # never awaited" message.
            self._coroutine.close()
        if getcoroutinestate(self._coroutine
                             ) is CORO_SUSPENDED and not self._closed:
            if sys.implementation.name == "pypy":
                # pypy segfaults if we resume the coroutine from our __del__
                # and it executes any more 'await' statements, so we use the
                # old async_generator behavior of "don't even try to finalize
                # correctly". https://bitbucket.org/pypy/pypy/issues/2786/
                raise RuntimeError(
                    "partially-exhausted async_generator {!r} garbage collected"
                    .format(self.ag_code.co_name)
                )
            elif self._finalizer is not None:
                self._finalizer(self)
            else:
                # Mimic the behavior of native generators on GC with no finalizer:
                # throw in GeneratorExit, run for one turn, and complain if it didn't
                # finish.
                thrower = self.athrow(GeneratorExit)
                try:
                    thrower.send(None)
                except (GeneratorExit, StopAsyncIteration):
                    pass
                except StopIteration:
                    raise RuntimeError("async_generator ignored GeneratorExit")
                else:
                    raise RuntimeError(
                        "async_generator {!r} awaited during finalization; install "
                        "a finalization hook to support this, or wrap it in "
                        "'async with aclosing(...):'"
                        .format(self.ag_code.co_name)
                    )
                finally:
                    thrower.close()


if hasattr(collections.abc, "AsyncGenerator"):
    collections.abc.AsyncGenerator.register(AsyncGenerator)


def async_generator(coroutine_maker):
    @wraps(coroutine_maker)
    def async_generator_maker(*args, **kwargs):
        return AsyncGenerator(coroutine_maker(*args, **kwargs))

    async_generator_maker._async_gen_function = id(async_generator_maker)
    return async_generator_maker


def isasyncgen(obj):
    if hasattr(inspect, "isasyncgen"):
        if inspect.isasyncgen(obj):
            return True
    return isinstance(obj, AsyncGenerator)


def isasyncgenfunction(obj):
    if hasattr(inspect, "isasyncgenfunction"):
        if inspect.isasyncgenfunction(obj):
            return True
    return getattr(obj, "_async_gen_function", -1) == id(obj)
