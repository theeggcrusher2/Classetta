"""
The 68k side of macrt: the CPU, the A-line trap dispatcher and the
Segment Loader, which together run an application's CODE resources.

    cpu       the processor: unicorn's m68k core on the guest RAM
    dispatch  A-line traps: decode the trap word, pop the arguments,
              call the Python implementation, push the result
    glue_*    trap implementations, named as in Inside Macintosh
    segload   CODE resources, the A5 world and the jump table
    runtime   a Process's main loop for 68k code
    trapdefs  every trap's name and calling convention (generated)
"""
