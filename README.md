# C++ TOOLS
This plugin is for you who want use feature like Visual Studio Code capability but use minimum memory footprint.

# Features
1. Completion
2. Hover documentation
3. Document formatting
4. Go to definition
5. Go to declaration
6. Code actions
7. Rename symbol

# Requirements
* **Sublime Text** build `4050` or later
* `MarkedPopup` sublime plugin. ( repo `https://github.com/ginanjarn/MarkedPopup.git` )
* `Clangd`. ( Clangd is part of LLVM Project. Install instruction on [LLVM home page](https://releases.llvm.org/download.html) )


> **COMPILE COMMANDS**
> 
> `clangd` requires `compile_commands.json` to provide scope context. If not defined, `clangd` will not working as expected.
>  
> Auto generate with `cmake` generator with added flag `-DCMAKE_EXPORT_COMPILE_command=TRUE`.
> Or, use  [CMakeTools](https://github.com/ginanjarn/CMakeTools) to generate cmake config from Sublime Text command.
