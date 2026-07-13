# -*- coding: utf-8 -*-

import click
import sys
import time
from pathlib import Path
from rich.console import Console

from .watcher import FSWatcher
from .__version__ import __version__

@click.command()
@click.argument('paths', nargs=-1, type=click.Path(exists=True, file_okay=True, dir_okay=True, readable=True))
@click.option('--recursive/--no-recursive', default=True, help="Watch directories recursively.")
@click.option('--debounce', default=0.05, type=float, help="Debounce interval in seconds.")
@click.option('--gitignore/--no-gitignore', default=True, help="Load ignore rules from .gitignore files.")
@click.option('--ignore', '-i', multiple=True, help="Extra ignore patterns.")
def main(paths, recursive, debounce, gitignore, ignore):
    """Monitor paths for file system changes."""
    console = Console()
    console.print(f"[bold cyan]yyds-fswatch v{__version__}[/bold cyan] starting up...")
    
    resolved_paths = []
    if not paths:
        resolved_paths = [Path('.').resolve()]
    else:
        resolved_paths = [Path(p).resolve() for p in paths]

    console.print(f"Watching paths: [bold]{', '.join(str(p) for p in resolved_paths)}[/bold]")
    console.print(f"Options: recursive={recursive}, debounce={debounce}s, gitignore={gitignore}")
    if ignore:
        console.print(f"Extra ignores: {list(ignore)}")
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    watcher = FSWatcher(
        path=resolved_paths,
        recursive=recursive,
        debounce=debounce,
        use_gitignore=gitignore,
        ignore_patterns=list(ignore)
    )

    @watcher.on('*')
    def handle_event(event):
        timestamp = time.strftime('%H:%M:%S')
        dir_flag = " [dir]" if event.is_directory else ""
        
        if event.event_type == 'created':
            color = 'green'
            action = 'CREATED'
            path_str = event.src_path
        elif event.event_type == 'deleted':
            color = 'red'
            action = 'DELETED'
            path_str = event.src_path
        elif event.event_type == 'modified':
            color = 'yellow'
            action = 'MODIFIED'
            path_str = event.src_path
        elif event.event_type == 'moved':
            color = 'blue'
            action = 'MOVED'
            path_str = f"{event.src_path} -> {event.dest_path}"
        else:
            color = 'white'
            action = event.event_type.upper()
            path_str = event.src_path

        console.print(f"[{timestamp}] [bold {color}]{action:<8}[/bold {color}] {path_str}{dir_flag}")

    try:
        watcher.start()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Stopping watcher...[/bold yellow]")
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
