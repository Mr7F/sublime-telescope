# Sublime - Telescope
Sublime text plugin that mimic the "live grep" feature of the [telescope](https://github.com/nvim-telescope/telescope.nvim) plugin from VIM.

That plugin work with [ripgrep](https://github.com/BurntSushi/ripgrep) and [fzf](https://github.com/junegunn/fzf)

It will **fuzzy find** in all files with a given extension, with an heuristic
- it will first use ripgrep with the 3 first characters and the 3 last ones
- it will apply fuzzy search on the result with fzf
- it will keep the 50 first results

<p align="center">
  <img src="img/demo.gif">
</p>

Debian / Ubuntu
> sudo apt install ripgrep fzf

MacOS
> brew install coreutils ripgrepfzf

Windows with choco
>  Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

> choco install ripgrep sed fzf

# Keybind
```json
{
    "keys": ["ctrl+i"],
    "command": "telescope"
}
```

# TODO
- Use quick panel once that issue is done: https://github.com/sublimehq/sublime_text/issues/4796
- Change "selected by default" when https://github.com/sublimehq/sublime_text/issues/5507 is merged
- get the x first result with rg instead of head
