from agent.config import auto_reload_enabled, port
from agent.platforms.line.app import app


if __name__ == '__main__':
    if auto_reload_enabled():
        app.run(host='0.0.0.0', port=port(), threaded=True, use_reloader=True)
    else:
        from waitress import serve

        serve(app, host='0.0.0.0', port=port(), threads=8)
