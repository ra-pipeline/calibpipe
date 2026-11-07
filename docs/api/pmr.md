# calibpipe.pmr

`calibpipe pmr --symlink-shortcuts` creates relative `working`, `rawdata`, and `products` shortcuts
at the project root when it finds a single nested MOUS working directory.
It also links `weblog` when pipeline HTML already exists. Shortcuts are created
only after successful execution and any requested project-root rename.
Existing real files and directories are preserved.

PMR shortcuts are disabled by default. Enable them with `--symlink-shortcuts`:
this is independent of `[run].symlink_shortcuts`, which controls `calibpipe run`.

```bash
calibpipe pmr uid://A001/X1/X1 --download --symlink-shortcuts
```

Pass `--procedure-enable-parallel` with `--procedure` to add `parallel=True` to supported tasks
in the generated `working/PPR*.xml` files, after any project-root rename:

```bash
calibpipe pmr uid://A001/X1/X1 --procedure procedure_hifa_calimage.xml --procedure-enable-parallel
```

This runs `scripts/procedure_add_parallel.py` from the pipeline checkout located
via `SCIPIPE_SCRIPTDIR` (`<checkout>/pipeline/recipes`). The script preserves
existing parallel parameter values. Conversion failures return a nonzero exit
code and preserve the affected original PPR. `--dry-run` previews the conversion
without changing files.

::: calibpipe.pmr
