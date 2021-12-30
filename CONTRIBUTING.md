### Setup

This project uses [`pip-tools`](https://github.com/jazzband/pip-tools) to manage
dependencies.

To get started, first create and activate a new virtual environment:
```
$ python3.8 -m venv venv
$ source venv/bin/activate
```

Then upgrade `pip` and install `pip-tools`:
```
$ pip install --upgrade pip
$ pip install pip-tools
```

Lastly, use `pip-sync` to install the dev requirements:
```
$ pip-sync requirements/requirements-dev.txt
```

### Formatting

This project uses [`isort`](https://github.com/pycqa/isort) and
[`black`](https://github.com/psf/black) to automatically format the source code.
You should invoke both of them, in that order, before submitting pull requests:
```
$ isort src/
$ black src/
```

### Linting

This project uses [`flake8`](https://github.com/pycqa/flake8) for linting, a
basic form of static analysis. You can use `flake8` to check for errors and bad
code style:
```
$ flake8 src/
```

### Type Checking

This project uses [`pyre`](https://github.com/facebook/pyre-check) to check for
type errors. You can invoke it from anywhere in the repository as follows:
```
$ pyre
```

Note that Pyre depends on [`watchman`](https://github.com/facebook/watchman), a
file watching service, for incremental type checking. It takes a few minutes to
install, but it's worth the investment - it makes type checking almost
instantaneous.
