| A-line traps the test applications use, as macros named as in Inside
| Macintosh.  The numbers come from macrt/m68k/trapdefs.py.

        .macro  trap name, word
        .macro  _\name
        .short  \word
        .endm
        .endm

        trap    InitGraf,       0xA86E
        trap    InitFonts,      0xA8FE
        trap    InitWindows,    0xA912
        trap    InitMenus,      0xA930
        trap    TEInit,         0xA9CC
        trap    InitDialogs,    0xA97B
        trap    InitCursor,     0xA850
        trap    FlushEvents,    0xA032
        trap    GetMenu,        0xA9BF
        trap    InsertMenu,     0xA935
        trap    AppendResMenu,  0xA94D
        trap    DrawMenuBar,    0xA937
        trap    GetNewWindow,   0xA9BD
        trap    SetPort,        0xA873
        trap    WaitNextEvent,  0xA860
        trap    BeginUpdate,    0xA922
        trap    EndUpdate,      0xA923
        trap    EraseRect,      0xA8A3
        trap    FrameRect,      0xA8A1
        trap    PaintOval,      0xA8B8
        trap    MoveTo,         0xA893
        trap    DrawString,     0xA884
        trap    FindWindow,     0xA92C
        trap    MenuSelect,     0xA93D
        trap    MenuKey,        0xA93E
        trap    HiliteMenu,     0xA938
        trap    TrackGoAway,    0xA91E
        trap    DragWindow,     0xA925
        trap    DisposeWindow,  0xA914
        trap    InvalRect,      0xA928
        trap    ExitToShell,    0xA9F4
        trap    Pack0,          0xA9E7
        trap    Pack7,          0xA9EE
        trap    Gestalt,        0xA1AD
        trap    NewGestalt,     0xA3AD
        trap    Launch,         0xA9F2
        trap    GlobalToLocal,  0xA871
        trap    NewControl,     0xA954
        trap    GetNewControl,  0xA9BE
        trap    DrawControls,   0xA969
        trap    FindControl,    0xA96C
        trap    TrackControl,   0xA968
        trap    GetControlValue, 0xA960
        trap    SetControlValue, 0xA963
        trap    TextFont,       0xA887
        trap    TextSize,       0xA88A
        trap    TENew,          0xA9D2
        trap    TEActivate,     0xA9D8
        trap    TEDeactivate,   0xA9D9
        trap    TEIdle,         0xA9DA
        trap    TEKey,          0xA9DC
        trap    TEClick,        0xA9D4
        trap    TEUpdate,       0xA9D3
        trap    TECut,          0xA9D6
        trap    TECopy,         0xA9D5
        trap    TEPaste,        0xA9DB
        trap    TEDelete,       0xA9D7
        trap    ZeroScrap,      0xA9FC
        trap    GetScrap,       0xA9FD
        trap    PutScrap,       0xA9FE

        .macro  _NumToString
        clr.w   -(sp)
        _Pack7
        .endm
        trap    PaintRect,      0xA8A2
        trap    GetNewDialog,   0xA97C
        trap    ModalDialog,    0xA991
        trap    GetDialogItem,  0xA98D
        trap    SetDialogItem,  0xA98E
        trap    GetDialogItemText, 0xA990
        trap    SetDialogItemText, 0xA98F
        trap    HideDialogItem, 0xA827
        trap    ParamText,      0xA98B
        trap    DisposeDialog,  0xA983
        trap    StopAlert,      0xA986
        trap    DialogDispatch, 0xAA68
        trap    InitProcMenu,   0xA808
        trap    GetMHandle,     0xA949
        trap    EnableItem,     0xA939
        trap    GetResource,    0xA9A0
        trap    VInstall,       0xA033
        trap    VRemove,        0xA034
        trap    Delay,          0xA03B
        trap    InsTime,        0xA058
        trap    InsXTime,       0xA458
        trap    RmvTime,        0xA059
        trap    PrimeTime,      0xA05A
        trap    Pack8,          0xA816
        trap    OSDispatch,     0xA88F
        trap    PPC,            0xA0DD
        trap    GetOSTrapAddress, 0xA346
        trap    SetOSTrapAddress, 0xA247
        trap    NewHandle,      0xA122
        trap    GetHandleSize,  0xA025
        trap    SetResLoad,     0xA99B
        trap    LoadResource,   0xA9A2
        trap    Open,           0xA000
        trap    Close,          0xA001
        trap    Read,           0xA002
        trap    Write,          0xA003
        trap    WriteAsync,     0xA403
        trap    Control,        0xA004
        trap    Status,         0xA005
        trap    KillIO,         0xA006
        trap    ControlAsync,   0xA404
        trap    StatusImmed,    0xA205
        trap    NewPtrSysClear, 0xA71E
        trap    DisposPtr,      0xA01F
        trap    NewWindow,      0xA913
        trap    GetNextEvent,   0xA970
        trap    FrontWindow,    0xA924
        trap    SelectWindow,   0xA91F
        trap    NewMenu,        0xA931
        trap    AppendMenu,     0xA933
        trap    DeleteMenu,     0xA936
        trap    DisposeMenu,    0xA932
        trap    CountMItems,    0xA950
        trap    GetMenuItemText, 0xA946
        trap    OpenDeskAcc,    0xA9B6
        trap    CloseDeskAcc,   0xA9B7
        trap    SystemEvent,    0xA9B2
        trap    SystemClick,    0xA9B3
        trap    SystemTask,     0xA9B4
        trap    SystemMenu,     0xA9B5
        trap    SystemEdit,     0xA9C2
