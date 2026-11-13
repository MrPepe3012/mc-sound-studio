import runpy, pathlib
runpy.run_path(str(pathlib.Path(__file__).with_name('server.py')), run_name='__main__')

