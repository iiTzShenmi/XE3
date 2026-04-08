from agent.features.e3.views import course_cards as _impl

globals().update({name: getattr(_impl, name) for name in dir(_impl) if not name.startswith('__')})
