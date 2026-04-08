from agent.features.e3.data import course_runtime as _impl

globals().update({name: getattr(_impl, name) for name in dir(_impl) if not name.startswith('__')})
