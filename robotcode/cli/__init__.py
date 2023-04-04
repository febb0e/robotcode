from pathlib import Path
from typing import List, Optional

import click

from robotcode.plugin import Application, ColoredOutput, pass_application
from robotcode.plugin.manager import PluginManager

from .__version__ import __version__
from .commands import config, profiles


@click.group(
    context_settings={"auto_envvar_prefix": "ROBOTCODE"},
    invoke_without_command=False,
)
@click.version_option(version=__version__, prog_name="robotcode")
@click.option(
    "-c",
    "--config",
    "config_files",
    type=click.Path(exists=True, path_type=Path),
    multiple=True,
    show_envvar=True,
    help="""\
        Config file to use. Can be specified multiple times.
        If not specified, the default config file is used.
        """,
)
@click.option(
    "-p",
    "--profile",
    "profiles",
    type=str,
    multiple=True,
    show_envvar=True,
    help="""\
        The Execution Profile to use. Can be specified multiple times.
        If not specified, the default profile is used.
        """,
)
@click.option("-d", "--dry", is_flag=True, show_envvar=True, help="Dry run, do not execute any commands.")
@click.option(
    "--color / --no-color",
    "color",
    default=None,
    help="Whether or not to display colored output (default is auto-detection).",
    show_envvar=True,
)
@click.option("-v", "--verbose", is_flag=True, help="Enables verbose mode.")
@click.pass_context
@pass_application
def robotcode(
    app: Application,
    ctx: click.Context,
    config_files: Optional[List[Path]],
    profiles: Optional[List[str]],
    dry: bool,
    verbose: bool,
    color: Optional[bool],
) -> None:
    """\b
     _____       _           _    _____          _
    |  __ \\     | |         | |  / ____|        | |
    | |__) |___ | |__   ___ | |_| |     ___   __| | ___
    |  _  // _ \\| '_ \\ / _ \\| __| |    / _ \\ / _  |/ _ \\
    | | \\ \\ (_) | |_) | (_) | |_| |___| (_) | (_| |  __/
    |_|  \\_\\___/|_.__/ \\___/ \\__|\\_____\\___/ \\__,_|\\___|
    A CLI tool for Robot Framework.

    """
    app.config.config_files = config_files
    app.config.profiles = profiles
    app.config.dry = dry
    app.config.verbose = verbose
    if color is None:
        app.config.colored_output = ColoredOutput.AUTO
    elif color:
        app.config.colored_output = ColoredOutput.YES
    else:
        app.config.colored_output = ColoredOutput.NO


robotcode.add_command(config)
robotcode.add_command(profiles)

for p in PluginManager().cli_commands:
    for c in p:
        robotcode.add_command(c)


@robotcode.command()
@click.pass_context
def debug(ctx: click.Context) -> None:
    """TODO: Debug a Robot Framework run.

    TODO: This is not implemented yet.
    """
    click.echo("TODO")


@robotcode.command()
@click.pass_context
def clean(ctx: click.Context) -> None:
    """TODO: Cleans a Robot Framework project.

    TODO: This is not implemented yet.
    """
    click.echo("TODO")


@robotcode.command()
@click.pass_context
def new(ctx: click.Context) -> None:
    """TODO: Create a new Robot Framework project.

    TODO: This is not implemented yet.
    """
    click.echo("TODO")
