# Sublime - Telescope
Sublime text plugin that mimic the "live grep" feature of the [telescope](https://github.com/nvim-telescope/telescope.nvim) plugin from VIM.

<p align="center">
  <img src="img/demo.gif">
</p>


It will search in files, ignoring the glob pattern defined in those settings:
- `binary_file_patterns`
- `file_exclude_patterns`
- `folder_exclude_patterns`

It use the "smart case" option from rigprep (if everything is lower case, then the search is case insensitive).

In the query, term that starts with `*` or `-*` are considered as glob filter
- `*.py`: only files with python extension
- `-*.py`: all files except python extension
- `*/models/*`: direct files under `models` folder
- `*/static/src/{js,css}/*`: any files under `/static/src/js` or `/static/src/css`
- `~`: search in your home directory instead of the current project

That plugin work with [ripgrep](https://github.com/BurntSushi/ripgrep)

> sudo apt install ripgrep

> brew install coreutils ripgrep

# Keybind
```json
{
    "keys": ["ctrl+i"],
    "command": "telescope",
},
{
    "keys": ["ctrl+."],
    "command": "telescope_find_next",
    "context": [{"key": "panel", "operand": "output.telescope"}, {"key": "panel_has_focus"}],
},
{
    "keys": ["ctrl+,"],
    "command": "telescope_find_prev",
    "context": [{"key": "panel", "operand": "output.telescope"}, {"key": "panel_has_focus"}],
}
```

# TODO
- ctrl+r to toggle regex
- publish
- Use quick panel once that issue is done: https://github.com/sublimehq/sublime_text/issues/4796
- get the x first result with rg instead of head
