# Running a Mac application

macrt runs classic Mac applications, the programs written for 68000-family
Macs and System 1 to 7, as windows on your desktop next to your own
programs. It reads the application's resources and loads its 68k code
into an emulated processor. Python answers the Toolbox and Operating
System calls that code makes, and PyQt6 draws the result.

This page covers what to install, how to get an application to macrt in a
form it can read, how to run it, and what to do when it stops.

- [Quick start](#quick-start)
- [What you need](#what-you-need)
- [Which applications run](#which-applications-run)
- [Getting the application to macrt](#getting-the-application-to-macrt)
- [Running it](#running-it)
- [What happens at launch](#what-happens-at-launch)
- [What works](#what-works)
- [When it stops](#when-it-stops)
- [Writing a test application](#writing-a-test-application)


## Quick start

```sh
python -m pip install -e .              # PyQt6, unicorn, rsrcfork, machfs
python -m macrt tests/apps/Hello.bin
```

Hello is one of the small test applications in `tests/apps`, written in 68k
assembly. It opens a window and puts up a menu bar; File ▸ Quit ends it, as
does ⌘Q (Ctrl+Q on a PC keyboard). If Hello runs, everything macrt needs is
installed.

Then run your own application:

```sh
python -m macrt MacPaint.bin
python -m macrt "System 6.0.8.dsk" --app TeachText
```

`pip install` also puts a `macrt` command on your path, so `macrt
MacPaint.bin` does the same thing. With no file, macrt runs its two
built-in demo programs instead.


## What you need

- **Python 3.11 or later.**
- **PyQt6** (6.5 or later) for the windows and menus.
- **unicorn** (2.1 or later), the 68k processor emulator.
- **rsrcfork** and **machfs**, which read resource forks and HFS disks.
  (MFS disks macrt reads itself.)

`pip install -e .` installs all four. unicorn has wheels for Linux, macOS
and Windows.

- **To unpack Mac archives** (StuffIt, Compact Pro and so on), get The
  Unarchiver's command-line tool, `unar`: `apt install unar` or `brew
  install unar`.
- **To run without a display** (a server or CI), set
  `QT_QPA_PLATFORM=offscreen`.


## Which applications run

| What you have | What macrt does |
| --- | --- |
| A 68k application: type `APPL` with `CODE` resources, as for a Mac Plus, SE, II, Classic, LC or a 68k PowerBook | runs it |
| A fat application, with both 68k and PowerPC code | runs the 68k code |
| A PowerPC-only application: a `cfrg` resource and no `CODE` | shows its menus, windows and dialogs from its resources, without running it |
| A self-expanding application, compressed by AutoDoubler | runs it: the application's own decompressor expands its code and resources as they load |
| An application built with MPW's far code model (a 32-bit jump table) | refuses it at launch |
| A desk accessory, in the System file or the application's own file | opens it from the application's Apple menu, or wherever the application lists it; it can't be run by itself |
| Control panels and extensions | can't run them yet |

How far an application gets depends on the Toolbox calls it makes (see
[What works](#what-works)). Programs that keep to the Toolbox do best:
word processors, utilities, and games that draw through QuickDraw.
Programs that need Color QuickDraw are less likely to work. Games from
the Mac's first years, which draw straight into the screen and keep time
by its hardware, run with `--host screen --size 512x342`; see [Games from
1984](#games-from-1984).

### Tried so far

These come from the System 6.0.8 disk image, a disk of System 6
utilities, games from the Info-Mac archive, and 400K floppies from the
Mac's first years: System 1.1, MacPaint, MacWrite, Microsoft Word 1.0,
Microsoft Multiplan, Chart and BASIC, MacDraft, Flight Simulator, a disk
of Mac 128K games and more games, and a third set from 1984 to 1987:
SuperPaint, MacWrite 4.5, VideoWorks, Wizardry, Infocom's Hitchhiker's
Guide, Duane Blehm's games and others; and a fourth, mostly games from
1985 to 1990: Déjà Vu, The Oregon Trail, SimCity, Infocom's
Masterpieces, Tom Snyder's simulations and more; and a fifth, from 1987
to 1992: Prince of Persia, Glypha, Ishido, Battle Chess, a disk of
MacGames and a compilation of Mac Plus games; and a sixth, from the
moof-a-day collection's MOOF images: Microsoft Excel 1.0, FullPaint,
Q-Sheet, Dollars and $ense, PFS:FILE, the Deluxe Music Construction
Set, Macintosh Pascal and PageMaker; and a seventh, more games from the
same collection: Falcon, Winter Games, The Ancient Art of War, Hacker,
Hacker II, Shufflepuck Cafe, Ferrari Grand Prix, Déjà Vu II, Dragonworld, The Duel: Test Drive II,
Temple of Apshai Trilogy, Where in Europe Is Carmen Sandiego?,
Sub Battle Simulator,
GATO, The Pawn, Tass Times in Tonetown, Wizardry II, Patton vs. Rommel
and others; and an eighth from it: Angelsoft's interactive fiction
(Goldfinger, The Mist, Rambo), Infocom's A Mind Forever Voyaging, Trinity
and Border Zone, First Byte's Fraction Action, MathTalk and KidTalk,
Physics, MacWars, Where in Time Is Carmen Sandiego?, Harrier Strike
Mission, HardBall, Earl Weaver Baseball, Wizardry VI, Moebius and others
(see [Where to find software](#where-to-find-software)).

| Application | What happens |
| --- | --- |
| TeachText | Runs: type and edit in its window. |
| Microsoft Excel | Runs: the worksheet, its menus and typing in cells, and quits, asking first whether to save a changed worksheet. |
| Creepy Castle | Plays, with its music and sound effects. Click through the introduction, then double-click a level to start it; the keypad's numbers move and fight. |
| Glider+ 3.1.2 | Plays, with its sounds. Start with Game > Begin Game. |
| Classic Daleks 1.2 | Plays, with its tunes. Start with File > New Game; the keypad's numbers move. |
| Fly Swatter | Plays. Start with File > New Game. |
| Vector 2.5 | Draws its 3D surface. It has no menus. |
| Font/DA Mover 4.1 | With a real System file (`--system`), lists its fonts and desk accessories, shows a sample of the font picked, and removes what you pick. With macrt's own System, which has neither, both lists are empty. |
| DA Piggyback | Shows its introduction. |
| PowerPCheck | File > Scan… asks for an application and reports its 68K and PowerPC code in a window, which File > Save As… saves. |
| LabelWriter II App | Asks for a label list at launch, then shows the label editor, the list and its fields. |
| PrintMonitor | Says it works only when MultiFinder is on, as it does on a Mac without MultiFinder. |
| Disk Copy 4.2 | Shows its introduction and the space it has to copy with. |
| Finder Info 1.1.1 | Asks for a file and shows its Finder information: type and creator, dates and flags. |
| ForkZapper | Asks for a file at launch and erases its data fork. The pop-up menu in its Open dialog that picks the resource fork instead isn't shown (macrt leaves out an application's own items in these dialogs), so it's always the data fork. |
| StuffIt Expander installer | Shows its introduction and its install dialog, and installs StuffIt Expander where you say, with its Read Us First! folder and an alias of it on the desktop. |
| Operation Tsunami | Plays, with its sounds, with `--model ii`. It's compressed by AutoDoubler, whose notice comes up first: click OK. Then click Play; W, A, S and D move, O shoots, P kicks and the space bar jumps. On the default Plus it draws straight into the screen's memory as if it were a 512×342 Mac's, so its picture lands in the wrong place unless it's run with `--host screen --size 512x342`. |
| MacPaint | Paints: the tools, patterns, brush shapes and line widths. Print Draft and Print Final print the picture to a PDF (see [Printing](#printing)). |
| Aliens | Plays. With `--host screen`, its playfield is the desk. |
| MacLanding | Plays, with `--host screen`, its radar drawn over the menu bar at the top of the screen, which it fills white, bar and all. |
| Frogger | Plays with `--host screen --size 512x342 --speed 1`. The keys are I, J, K and L, as capitals: hold Shift or turn Caps Lock on. |
| Airborne! | Plays, with its sounds, with `--host screen --size 512x342`: Start Game, and helicopters fly over dropping paratroopers. Holding the button down fires the gun toward the pointer, and hits score. |
| Crystal Raider | Plays, with `--host screen`. A click or key leaves the introduction for the Hall of Fame, and another starts the game. |
| MacWrite | Types and edits: lines wrap, Return starts a paragraph, and the Style menu's bold and italic work. Documents save, in either of the Save dialog's formats, and open again. Page Setup, printing and quitting work. |
| Microsoft Word 1.0 | Opens an Untitled document and types, with its lines wrapped at the margins. Selecting, the Character and Paragraph menus, the ruler and the Formats… dialog work. Saving and printing work. |
| Microsoft Multiplan | Runs: click a cell to select it, then type a number or a formula, such as `=R1C1*3+1`, in the formula bar; Return enters it and moves down a row. |
| Font Mover (System 1.1) | Lists the fonts in the Fonts file next to it and shows a sample of the one picked, in the font's own bitmap, Cairo's pictures and all; Copy puts it in the System file. Help shows its two pages. macrt's own System has no bitmap fonts to list (see `--system`). |
| Disk Copy (System 1.1) | Says it works only on a 128K Macintosh, as it does on any later Mac. |
| Desk accessories (System 1.1) | With `--system` given System 1.1's disk, MacPaint's Apple menu lists them, and each opens in a window of its own: the Alarm Clock and the Control Panel show the date and time, Calculator calculates, Key Caps shows the keyboard and what's typed, Note Pad shows its notes, Puzzle slides its tiles and the Scrapbook shows its first page. |
| Microsoft Chart | Charts: type numbers in the New Series window's Y column and tick Plot Series to chart them. The Gallery menu's charts work, a pie with its percentages among them. |
| Microsoft BASIC | Runs programs: type lines in the Command window, then RUN; the output goes to Untitled. It reads about 15 keys ahead and beeps past that, as on a Mac, so the end of a long paste is lost. |
| MacDraft 1.2a | Draws: pick a tool in the palette, then drag in the drawing for rectangles, ovals and lines; the text tool types. |
| Lode Runner | Plays: Game > Start Game (Cmd-S), then the keyboard moves the runner (Options picks the keyboard or the mouse). With no game started it plays its demo. |
| Klondike 4.0 | Plays: OK the About box and press Play in the Scores window; click the stock to turn cards and drag them to the foundations and columns. |
| Rogue | Plays: name the character, then move with the keys h, j, k and l, or click where to go. |
| Fusillade | Plays: OK its introduction, then File > Play. I, J, K and L move; E, S, D and F shoot. |
| ShufflePuck | Plays. Game > new game, then click to serve; the paddle follows the mouse, and swiping the mouse up the window hits the puck. Demo > play demo plays itself. |
| Dark Castle | Plays, with `--host screen --size 512x342`. Its launcher hands over to its Data A file, which shows the title screen; click it, and the Great Hall comes up, Duncan walking with the arrow keys. It draws each frame in one screen buffer while the other shows, and flips between them through the VIA. Its title screen plays Bach's Toccata in D minor, which it mixes into the sound buffer itself. |
| Flight Simulator 1.02 | Runs with `--host screen --size 512x342`: the cockpit, the view of Meigs Field and Chicago, the clock and the radios. It moves its own cursor by how far the mouse moves, as on a Mac, so the cursor it draws doesn't sit under the host's. |
| MacWrite 4.5 | Types, with the fonts, sizes, styles and alignments of its menus and ruler; Save As saves. It needs the default `--model plus`: it pops a 68000's exception frames itself. |
| SuperPaint 1.0 | Opens Untitled between its tool and pattern palettes, one window its own `WDEF` shapes, and draws with the tools and patterns. It keeps the Clipboard File in the System Folder, which macrt writes. |
| VideoWorks | Asks for its master disk: its copy protection. |
| Pyramid of Peril | Plays: Move Forward, Turn Left and Turn Right walk the corridors, and the map beside the view follows. |
| Psion Chess | Plays: click a piece, then the square to move it to, and the computer answers. Pick it with `--app "Psion Chess"`. |
| Grid Wars | Plays, with `--host screen --size 512x342`: click an opponent, then the ship follows the mouse along the bottom and a click fires toward the cross. |
| Wizardry I | Plays: drag characters from the Roster to the Party window, double-click Maze in The Castle and answer the stairs; then F moves the party forward and L turns it left. |
| Hitchhiker's Guide to the Galaxy | Plays: type commands at the prompt. Pick it with `--app "Hitchhiker's Guide"`. |
| Dungeon of Doom 5.4, The Dungeon Revealed | Play: click the title away, choose File > New Game… (New… in The Dungeon Revealed), name the character, press Onward! and name the saved game. Then click beside the hero to move. |
| Enchanted Scepters | Plays: type commands such as `look` or `go south` and press Return; the room's picture and text follow, in the frames its own `WDEF` draws. Cmd-I lists what you carry. |
| IAGO | Plays Othello: Start a Game, then click a square to move, and it answers. |
| Duane Blehm's games | Play; pick each with `--app`. StuntCopter: Begin, then the mouse flies the helicopter and a click drops the stuntman. CairoShoot: Game > Begin. ZeroGravity: click the Pod for free flight; the pilot then drifts the way the mouse is from (320, 239) on the screen, faster the further it is, and stops with the mouse there. PUZZ'L: Continue, File > Open "Cat Puzzle", then drag pieces from the strip to the board. |
| Space Invaders | Plays: click the Play box at the top right to start. |
| MacBASIC | Runs programs: File > New, type the program in its Source window, then Program > Run. |
| Gemstone Warrior | Plays: Gemstone Warrior > Start Beginner Game, and after the temple scene I, J, K and L move the warrior a step for each press. Options > Redefine Keyboard lists the keys, in the Chicago it brings, double arrows and all. |
| Déjà Vu | Plays: it opens on the Bathroom Stall, with the scene, its exits, the inventory, the command buttons and the story so far. Click a command, then what it acts on in the scene or the Exits window, and the story answers. |
| Leisure Suit Larry | Plays: Return past the credits, click, type an age and Return, then answer the quiz by clicking the letters; the questions are picked at random. Then click to walk Larry, and type commands such as `look` and press Return. |
| The Oregon Trail | Plays: Travel the Trail, Continue, then type a name, pick an occupation and OK. Buy at Matt's General Store, Tab moving between the amounts, then pick a month, and Continue sets the wagon off along the trail. |
| Infocom's Masterpieces | ZORK I, ARTHUR, BEYOND ZORK and ZORK ZERO play: type commands such as `open mailbox` at the prompt, each followed by Return. Pick each with `--app`. ARTHUR shows its title picture until a key is pressed, then asks whether to restore a saved game (N); BEYOND ZORK wants BEGIN, then a character; ZORK ZERO draws its framed pages with their pictures, and a key turns past each [MORE]. The disk's TADS Run-Time 2.2 asks for a game in its Open dialog; the Winners folder has THE_ONE.GAM, TOONESIA.GAM and ZEBULON.GAM, and Toonesia plays, typed at its prompt. |
| SimCity 1.11 | Start a scenario: click Dullsville and click through its story to the city, its tool palette and the map. It then asks for a city's population from the manual's table, as its copy protection; a wrong answer is asked again. |
| Scarab of Ra 1.4 | Plays, with `--app "Scarab of RA 1.4"`: the arrows in the Movement window step and turn through the pyramid, and the view, the news, the inventory and the status follow. |
| SmoothTalker 2.1 | Opens Untitled and types. Speech > Speak Text speaks the text, through its own speech drivers, which write the sound buffer. |
| Wizard's Fire | Plays: Game > Start Game, and the missiles fall on the towns. MicroGames > Pressure and Brickbat, its own desk accessories, open in small windows of their own. With `--system` given the game's disk, Calculator opens from the Apple menu and works, by click or typed key. While a desk accessory is in front, the game passes its Game menu to it as the Edit menu, as on a Mac: click the game's window before Game > Quit. |
| Run for the Money | Plays, with `--host screen --size 512x342`: click past the introduction, then New Game and a skill level, and the two players' factories come up with their machines and cash. |
| Make Millions | Plays, with `--host screen --size 512x342`: Play New Game, type a name and Okay, then click to start. The office's clock runs through the week, and clicking the telephone opens it, asking for a number. |
| ADVENTURE (James Associates) | Plays: answer its question about instructions, then type commands such as `look` and `go north` at its prompt, each followed by Return. |
| ][ in a Mac | Starts its Apple II: ProDOS 8's start screen, its keyboard and its disk drives. |
| Bird Race | Plays: click Race and the birds fly across the picture, at the odds beside them; the players' bets go in the table below. It reports "SpeechOn Error = -43" in the menu bar, as a Mac without MacinTalk does. |
| Social Climber | Plays: Game > Begin, then S and D walk the climber left and right; Game > Set Keys changes them. |
| Daleks | Plays: click an arrow beside the Doctor to move him that way, and the Daleks close in. The Game menu has Teleport, Last Stand and the Sonic Screwdriver. |
| Connect Four | Plays: click the oval above a column to drop a piece, and the computer answers. Game > NewGame starts again, with a few grey neutral pieces dropped at random, as the game does. |
| Brickles 7.0 | Plays: click to begin, and the mouse steers the paddle. |
| Microsoft BASIC 2 | Runs programs: type the program in the List window, then Run > Start; the output goes to Untitled, and the listing stays. |
| Skyfox | Runs with `--host screen --size 512x342`: its launcher chains to the game, whose title screen a click passes for the cockpit, its clock running, and a click on the cockpit's keyboard brings up the tactical map of the enemy's tanks. |
| Mac Attack | Runs with `--host screen --size 512x342`, from its MOOF image, and passes its copy protection, which times the disk turning under the head. Choose a level of difficulty, and the game plays its title and its attract mode, its tanks, missiles and landscape drawn in the fonts its disk's System file has. |
| Balance of Power: The 1990 Edition | Plays, with `--host screen --size 512x342`: New Game, then Start Game, and the world map comes up. Click a country to pick it; the Make Policies menu's dialogs then act on it. The title's letters are traced from the New York 36 the game carries, which macrt draws from rather than a host font. |
| Pirates! | Plays, with `--host screen`: click twice past the title, then answer its questions with clicks; any month answers its copy protection. The duel and the story follow. |
| Crystal Quest 2.2.5 | Plays, with its sounds, with `--host screen` and `--app "Crystal Quest"`: Return, then a click starts it, and the mouse steers the ship. |
| The Colony | Plays, with `--host screen`: click twice, then hold the button down to turn and press W to walk. |
| Cap'n Magneto | Plays, with `--host screen --size 512x342`: click Game, then the arrow keys walk. |
| Mac Missiles | Plays, with `--host screen`: click 1 Player, then click to fire at the missiles. |
| PhrazeCraze Plus | Plays, with `--host screen`: click through to Play, then Spin and pick letters. |
| Radical Castle | Plays, with `--host screen --size 512x342`: click Start. |
| Citgo, BackBay 2.0 | Show Kenmore Square. The first one's name starts with a space: `--app " Citgo"`. |
| Amazing | Plays with `--size 512x342`: the ball follows the mouse through the maze. The game sizes its maze from the screen but keeps it in arrays made for the original Mac's, so on a larger screen it runs past them and hangs on a grey screen. |
| The Quest | Plays, with `--host screen --size 512x342`. The game is its disk's Finder, which macrt runs, the disk holding no application. Press keys through the story, then type commands such as `n` at the prompt, or click the compass. |
| Prince of Persia | Plays: the introduction runs, with its music, Cmd-N starts the first level, and the arrow keys move the prince. |
| Glypha 3.0 | Plays: Cmd-B begins, then Space flaps and A and S steer (or ; and '). It needs a screen of 640x480 or more. Its disk has its sound off: Options › Configure Game turns it on. |
| Ishido | Plays, with `--app "Ishido (B&W)"`: Cmd-N deals, and a click places the stone in hand. |
| Battle Chess | Plays: click a piece, then the square to move it to, and it walks there; the computer answers. Its window needs a screen of 640x480 or more. |
| Kid Pix | Runs: its painting window, the tools and their options. |
| Macman Pro | Plays, with `--host screen`: Cmd-P starts it. |
| MacChess 2.0 | Plays, with `--model ii` and `--app "MacChess 2.0 EN"`: click a piece, then the square to move it to, and it answers, the move listed in its Moves window. On the default Plus it says it needs a 68020, as it does on a Mac. |
| Mombasa 1.1 | Shows its board of tiles; a click picks one and names it. |
| BlackJack, Backgammon, Hearts 1.6 | Play. BlackJack deals once a bet is clicked; Backgammon sets out its board once its introduction is OKed; Hearts deals and asks for three cards to pass. |
| Missile Mac, MissileCommand 3.0, Mac Pong II, Mine Sweeper | Play. |
| Railroad Tycoon 1.01 | Plays: the arrow keys and Return (or clicks) choose through its setup, the area and the difficulty and reality levels, and the world builds in under a minute. It asks for a locomotive to be named, from the manual, and the railroad opens with RailNews Weekly and the map. |
| Shadow Keep 2.2 | Plays, with its sounds: a click goes into the game, and holding the button down walks the hero toward the pointer. |
| Castle of Ert, Pyramid of Ert | Play: click through the title (Pyramid of Ert has a Start the Game button), then type commands such as `n` or `look`. |
| Capitalist Pig | Runs to its copy protection, which asks for a figure from the manual's Appendix A. |
| BiPlane | Runs: OK its shareware notice, then click a cell and type. |
| DataFlow | Runs: pick a block from the palette and click in the DataFlow window to place it. |
| MineSweeper 1.3 | Plays, its opening sound first: once its notice has gone, pick a size from the Game menu and click squares. |
| Forty Thieves 2.1, Reversi, Surprise Avond | Play: Forty Thieves deals from its stock with a click, Reversi begins with a click, and Surprise Avond's puzzles (Anagrams, Arithmetic, Fifteen-A-Row and more) take clicks. |
| Frenzy | Plays: File > New game starts a round. |
| MacBugs! | Plays: MAC BUGS! > Start game, then click GO. |
| Microsoft Excel 1.0 | Runs from its original disk's MOOF image, which passes its copy protection, and opens Worksheet1. |
| FullPaint 1.0 | Runs: its tools, patterns and an untitled drawing. It decrypts its segments as they're loaded, through a patch on `GetResource`, and runs part of itself in the processor's trace mode. |
| Q-Sheet 1.0 | Runs from its key disk's MOOF image, which it reads through bits that read differently each time: a cue list, the track list and the SMPTE time. Its objects are Object Pascal's, through `_MethodDispatch`. |
| Dollars and $ense 1.3, PFS:FILE | Run from their key disks' MOOF images, which they read with data marks of their own through the ROM's `RdData`: Dollars and $ense to its title and menus, PFS:FILE to its New, Open and Quit choices. |
| Deluxe Music Construction Set | Runs: an untitled score and the piano keyboard. |
| Macintosh Pascal 1.0 | Runs: type a program in its editor, which shows keywords in bold and indents as it goes, then Run > Check and Run > Go. `writeln` writes to the Text window and QuickDraw's calls draw in the Drawing window; a mistake brings up its bug alert. It draws and measures its program text through a port's own `textProc` and `txMeasProc`, and calls TextEdit's `TEDoText` routine itself. |
| PageMaker 1.2 | Runs, with both of its MOOF images (the application is on the second), and passes its copy protection, which steps the head a track in from its key sector and counts the bytes there as the disk turns. File › New, then OK in Page setup, opens a publication with its toolbox, whose icons are its own font's, a family numbered past 255. |
| The Home Accountant 1.01 | Runs, with `--host screen --size 512x342`, from its MOOF image, and passes its master-disk check: it asks whether to open an existing Home Accountant system or create a new one. |
| Microsoft File | Runs from its MOOF image and passes its master-disk check, which looks for a flag in two files' catalog entries. File › Open Datafile… opens the sample Good Restaurants, its records with their pictures, and Organize › Find… shows the form to search with. File › New Datafile… takes the new file's name in an edit text item of its Save dialog. |
| Lotus Jazz 1A | Runs, with both of its MOOF images (`00playable.moof` first) and `--app Jazz`, and passes its copy protection, which reads its key disk's bytes through the ROM's routines and times them with the VIA's timer as the disk turns them. File › New opens a worksheet that calculates, a word-processing document, a graph or a database. |
| The Print Shop 1.2 | Runs, with both of its MOOF images (`00playable.moof` first), `--app "The Print Shop" --host screen --size 512x342`, and passes its copy protection, which sets the IWM's mode to read its disk's last track. That track was never imaged, so the drive reads noise there and the program uses the key it stored when it was installed. Its menu offers Greeting Card, Letterhead, Banner and Sign; double-click one to open it, then pick a border and type the message. |
| Fontographer 2.4.1 | Runs, with `--host screen --app Fontographer`, from its MOOF image `00playable2.moof`. At launch it asks for a font: open DemoFont, from its disk, and it shows the font's characters in a window; double-click one to edit its outline. File › New Font asks for the font's attributes and opens Untitled-1. Generate PostScript writes the font and its AFM file to the disk in a few seconds. Generate Bitmaps stays dimmed. |
| PFS:REPORT | Runs from its MOOF image: open a PFS:FILE file from its disk, such as STAFF, and Count forms counts its forms. |
| MusicWorks 1.0 | Runs from its MOOF image: File › Open plays a song from its disk, such as Alla Turka, through the Sound Driver's four-tone synthesizer, its Overview's cursor going through the score. |
| Master Tracks Pro 1.10 | Runs, with `--size 640x480`, from its MOOF image, and passes its key-disk check, which it makes through the Sony driver's control calls. File › Open opens a sequence from its disk, such as Banana Shuffle, and Play counts its measures and beats with its own routine for the VIA's timer 1, which it puts in low memory's Lvl1DT. Its MIDI goes out through the serial chip (SCC), which macrt doesn't have, so it plays without a sound. |
| Crossword Magic 4.0 | Runs from its MOOF image: File › Play… opens a puzzle from its disk, such as CHRISTMAS. |
| The Toy Shop 1.1 | Runs, with its Master Disk and Toy Disks 1 and 2, the Master Disk first. The toy disks are in the archive's zip, not among its MOOF images. Its Catalog and Index show the toys; open one, such as the Carousel, and pick its paints. |
| The Fool's Errand 2.0 | Runs: its story opens on The Sun's page, and its menus, -I- to -V-, list the puzzles. |
| Beyond Dark Castle | Plays, with both of its disks, `--host screen` and `--app "Beyond Dark Castle"`: its title, the Great Hall, then Play takes the player into the Ante Room, read from the second disk. |
| Uninvited | Plays, with both of its disks and `--host screen --size 512x342`: its copy protection decrypts code it reads straight from the disk, and it finds its second disk through a volume control block of its own. Click past the introduction in the car, then pick a command and click an object or an exit: Open on the car's exit opens its door. |
| King's Quest | Plays, with both of its disks and `--host screen --size 512x342`: its copy protection reads a file with a damaged sector and counts on the File Manager's I/O error. Press a key past its credits, then type a command and Return (`look at castle`), or walk Sir Graham with the arrow keys. The game draws him and the room straight into the screen, in light dithers. |
| The Seven Cities of Gold | Plays with `--host screen --size 512x342`. Click past its title. Choose items by moving the mouse up or down and clicking. When it asks for the map disk, it ejects its own disk and gets it back. Pick an experience level. Then leave Spain's harbour and sail west from the Canaries with the arrow keys to land in the New World. |
| The Chessmaster 2000 | Plays, with `--host screen --size 512x342`: it opens on its board. Drag a piece to move it, and the Chessmaster answers; each side's moves and clock are listed beside the board. The game interrupts itself with its own copy of the ROM's handler, which calls the ROM's routines that low memory's Lvl1DT points at. |
| MacGammon | Plays, with `--host screen --size 512x342`, from its MOOF image, and passes its copy protection, whose own copy of the ROM's routine reads its key sector and decodes the disk's bytes through the ROM's table at A3. Click START, then File › Start Game: the cups roll for who goes first, the roll shows under the board, and the checkers move. |
| Archon | Plays, with `--host screen --size 512x342`, from its MOOF image: click past its title for the Strategic Board. Click one of Light's pieces, which the game names, then the square to move it to, and the computer, playing Dark, answers. |
| Mouse Stampede | Plays, with `--host screen --size 512x342`, from its MOOF image: One Player, then Start, and the mice wind down through the cheese. The mouse steers the shooter and its button fires. |
| Sargon III | Plays, with `--app "Sargon III" --host screen --size 512x342`, from its MOOF image: drag a piece and Sargon answers, the moves listed in its PLAYER/SARGON window. It drags its pieces with `DragTheRgn`. |
| Shadowgate | Plays, with both of its disks and `--host screen --size 512x342`. Its second disk is in the archive's zip, not among its MOOF images. Click to continue through the opening text, and the command buttons come up. |
| Zork I | Plays, with `--host screen --size 512x342`, from its MOOF image: type a command and Return (`open mailbox`, `read leaflet`), and the score and moves update. |
| Shanghai | Plays, with `--host screen --size 512x342`, from its MOOF image: click two matching tiles to take them away; a mismatch says so. |
| Arkanoid | Plays, with `--host screen --size 512x342`, from its MOOF image. Click for its Start New Game dialog and pick One Player Start. After its introduction it asks for the round to start on, which moving the mouse changes. |
| Oids, Tesserae | Play, with both of their MOOF images (the application is on the second) and `--host screen --size 512x342`: Oids' Play warps to the Cosmoids galaxy, and Tesserae's Start opens its picker of mosaics, with its sounds. |
| Squire 1.1 | Runs, with `--host screen --size 512x342`, from its MOOF image: press P and Return, and it asks for your initials. |
| Pinball Construction Set | Runs, with `--host screen --size 512x342`, from its MOOF image: its editor, where parts drag from the box onto the table. |
| Falcon | Runs, with both of its MOOF images (`00playable.moof` first) and `--host screen --size 512x342`. Its first application plays the title, then Launches the game on the other disk by its full pathname, `Falcon 2:Falcon`, and the game opens on its Duty Roster. |
| Winter Games | Runs, with `--host screen --size 512x342`, from its MOOF image: past its title and opening ceremony, its menu of events. It makes a heap zone of its own with `InitZone`. |
| The Ancient Art of War | Plays, with `--host screen --size 512x342`, from its MOOF image. Game › Go to War lists its campaigns: double-click one, then Done › Continue past its briefing, rules and choice of opponent to the battlefield. Its check of its original disk counts the bytes on a track whose bytes are written four zeros apart. |
| Hacker | Runs, with `--host screen --size 512x342`, from its MOOF image, past its copy protection: type at its LOGON PLEASE: prompt and press Return, and the system you've reached answers (`? LOG-F-SYNTAX ERROR`). |
| Hacker II | Runs, with `--host screen --size 512x342`, from its MOOF image. It dials for about 20 seconds before its LOGON PLEASE: prompt: type HELP and Return for its terminal's commands, or a name and Return, and ACTISOURCE's hotline answers. It types its text a letter every few ticks, waiting for the Ticks to change and then looking for a key with `GetNextEvent`, which returns at once when it has nothing to give, as on a Mac. |
| The Duel: Test Drive II | Plays, with `--host screen --size 512x342`, from its MOOF image: its title, top scores and RACE AGAINST menu, its skill levels, and a race against the clock in its Ferrari's cockpit, with the scenery, the rear-view mirror and the gauges. It looks for its disk in the drive queue, to tell a floppy from a hard disk. The arrow keys drive, or the keypad's 8, 2, 4 and 6: Up speeds up, Down brakes, and Left and Right steer down the road, past its traffic and its speed limits. |
| Shufflepuck Cafe | Plays, with both of its MOOF images (`00playable.moof` first) and `--host screen --size 512x342`, past its copy protection, and opens in the bar with the Current Champion on the board. Its own routine reads a key sector as the floppy controller shifts the bits in, and partway through a byte it stops the shifting for as long as its own instructions take. The bytes after are its key only if as many bits go by unread as would on a Mac. |
| Ferrari Grand Prix | Runs, with `--app "Ferrari Grand Prix" --host screen --size 512x342`, from its MOOF image: give a name for its Famous Drivers and a level, then OK, and its cockpit comes up on the track, with the laps and times below. |
| Déjà Vu II | Plays, with `--app "Lost in Las Vegas" --host screen --size 512x342`, from its MOOF image. It opens as Déjà Vu does, in the Bathroom, with the story so far and its inventory, Exits and command windows. |
| Dragonworld | Plays, with both of its MOOF images (`00playable.moof` first) and `--host screen --size 512x342`, past its copy protection. After its credits it shows the story and the city of Kandesh while its music plays, then its title: press G to start, any key past `<MORE>`, then type a command and Return. Its own routines find a key sector and count what its key track has. |
| Temple of Apshai Trilogy | Plays, with `--host screen --size 512x342`, from its MOOF image. It has no Apple menu. Click past its title; Character › New Character, then CREATE, rolls a character to name. Game › Temple of Apshai asks which level, and the first room comes up beside the character's wounds and fatigue. Type a number to move that many steps; the BATTLE and SPECIAL menus, or the letters they list, fight and search. |
| Where in Europe Is Carmen Sandiego? | Plays, with both of its MOOF images (`00playable.moof` first) and `--host screen --size 512x342`. Click past its title, and its robot scans for about 20 seconds before it asks for your name: type it and Return, and answer its question with y or n. The Chief calls with the case, and the chase starts in the city of the crime, with its clues, the travel and investigation icons and the crime computer. |
| Sub Battle Simulator, GATO, Strategic Conquest | Play, with `--host screen --size 512x342`, from their MOOF images: Sub Battle's status report and periscope, GATO's control panel and gauges, Strategic Conquest's map and its Game Set-Up. |
| The Pawn, Tass Times in Tonetown, Borrowed Time, Transylvania | Play, with `--host screen --size 512x342`, from their MOOF images (The Pawn has two): type a command and Return. Tass Times and Borrowed Time show their pictures, and Transylvania asks you to sign its guest register. |
| Wizardry II, Alter Ego, Bridge 6.0, Mac Vegas, Patton vs. Rommel, One on One, Star Trek: The Kobayashi Alternative | Run, with `--host screen --size 512x342`, from their MOOF images: Wizardry II's Castle and Roster, Alter Ego's New Game, Bridge's opening deal, Mac Vegas' table of games, Patton vs. Rommel's choice of side, One on One's court and Star Trek's orders. |
| Apache Strike | Plays, with `--host screen --size 512x342`, from its MOOF image: Play, and after its briefing the Apache flies down the city's streets, with its radar, gauges and score beside. It draws its flight where a Mac Plus has its two screens, at the top of 4 MB, and flips between them through the VIA; here those addresses are the 512x342 screen's. It starts its engine's sound, of over two minutes, with an immediate write to the Sound Driver, which doesn't wait for it to end. |
| Frogger, Defender of the Crown, Mindshadow | Play, with `--host screen --size 512x342`, from their MOOF images: Frogger's buttons start a game, and Defender of the Crown tells its story in Sherwood Forest and has you choose a character. Mindshadow asks for 1 or 2: press 1 twice, as the first goes by while its Activision screen waits, and it opens on its beach, where you type a command and Return or pick from its Verbs and Nouns. |
| Indiana Jones and the Last Crusade | Runs, with both of its disk images (the System disk, `00playable.dc42`, first) and `--host screen --size 512x342`: the Lucasfilm Games logo, its title and credits, then Escape skips to Barnett College, and a click takes Indy indoors to Marcus. Its window covers the whole screen, the menu bar too, as it puts the bar back in its window's visRgn itself. |
| Fokker Triplane Flight Simulator | Plays, with `--host screen --size 512x342`, from its MOOF image. Pick a flight from the Game menu, Touch'n Go's, Dogfight or Self Fly, then click in the cockpit to take off: the mouse is the stick, 2 and 1 change the throttle, Space fires and Backspace ends the flight (Game › Change Keys changes them). With `--system` given the game's disk, the Scrapbook in the Apple menu holds its help pages. Game › Help opens the fourth item of the Apple menu, which was the Scrapbook on a 512K; here it's the Calculator, as a Mac Plus lists desk accessories in alphabetical order. The game moves its globals (its A5 world) below $70000 before it calls `InitGraf`, and desk accessories find QuickDraw's globals there. |
| Goldfinger, The Mist, Rambo, Forbidden Castle, Indiana Jones in Revenge of the Ancients | Play, with `--host screen --size 512x342`, from their MOOF images. Each disk has Angelsoft's MiniFinder and the game's own program beside it, so name the game with `--app`: `Goldfinger`, `"The Mist"`, `Rambo`, `Forbidden` or `"Indiana Jones™*"`. The story comes a window at a time, (MORE) between, then type a command at the `->` prompt and press Return. |
| A Mind Forever Voyaging, Trinity, Border Zone | Play, with `--host screen --size 512x342`, from their MOOF images: type a command and Return. Border Zone asks first which of its three chapters to play. |
| Fraction Action, MathTalk, KidTalk | Run, with `--host screen --size 512x342`, from their MOOF images, past their copy protection, whose key track reads differently each time it's read (see [Disk images](#disk-images)): Fraction Action's story of the mad professor's mansion, then How many Players?; MathTalk's NEW USER and MAIN MENU; KidTalk asks whether you've used it before. |
| Physics 1.2 | Runs, with `--host screen --size 512x342`, from its MOOF image: a dialog of its chapters and their lessons, and a lesson that draws and animates, as Vectors' arrow does. |
| MacWars | Plays, with `--host screen --size 512x342`, from its MOOF image: the starfield and crosshairs, with shields and score. |
| Alternate Reality: The City, Where in Time Is Carmen Sandiego?, The Crimson Crown, Harrier Strike Mission I and II, HardBall, Earl Weaver Baseball, Championship Star League Baseball, Smash Hit Racquetball, MacMatch, Maze Survival, Millionaire, Murder by the Dozen, OrbQuest, Pensate, Speed Reader II | Run, with `--host screen --size 512x342`, from their images (Alternate Reality's program is `--app Start`): Alternate Reality draws its city; Carmen Sandiego's ACME Detective Agency and its elevator; The Crimson Crown's lake shore with its buttons and text; Harrier Strike Mission's missions and cockpit gauges; HardBall's game options; Earl Weaver Baseball asks for the season ticket pass number of a seat it names; Star League Baseball's mode menu; Racquetball asks for your name; MacMatch's title and menus; Maze Survival's skill levels; Millionaire's player directory; Murder by the Dozen's twelve cases; OrbQuest's story; Pensate's board and pieces; Speed Reader II's exercises. |
| Sierra Championship Boxing, Fast Break, Geometry, Vegas Video Poker, The Mind Prober | Run, with `--host screen --size 512x342`, from their MOOF images (Geometry has two, `00playable.moof` first): Boxing's list of the boxers to pick from, Fast Break's court and scoreboard, Geometry's dialog of its chapters, and Vegas Video Poker's index after Return. In The Mind Prober, File › New Subject… asks for the subject's initials, and OK starts its questionnaire, PROBING YOUR SUBJECT. |
| The King of Chicago | Plays, with `--app "The King" --host screen --size 512x342`, from its MOOF image. Its only menu is Projector: Start the Show, and Pinky thinks in balloons. Click one to choose what he says. |
| Downhill Racer | Plays, with `--host screen --size 512x342 --speed 1`, from its MOOF image: from the lodge to the slope, where the mouse steers. At that speed its title comes up after about 12 seconds, and its skiers after about 18. It divides by the ticks its first frame took to draw, which on a Plus is at least one, so without `--speed` it stops with a zero divide (system error 4). Its copy protection reads its key track with its own RdData, which is called with interrupts masked, as the Disk Driver has them. To make the interrupts it doesn't want do nothing, it looks past Dequeue's code for the ROM's next RTE, and then gives up if the Time has moved on 3 seconds: there's an RTE soon after the traps' code, as in the ROM. Its sounds are the Sound Driver's queue at SoundDCE, which its StartSound glue changes. |
| Wizardry VI: Bane of the Cosmic Forge | Runs, with its six disks, `--app Bane --host screen --size 512x342`, from its MOOF images, to its Master Options. It ejects and unmounts its first disk and asks for disk B: choose `Bane "B"` in the dialog and press Return. Press a key at its title, then it asks for disk A again. |
| Moebius: The Orb of Celestial Harmony | Runs, with `--host screen --size 512x342`, from its MOOF image, past its copy protection, to its title and the story after. The check finds sector 0's address field on a track, steps the head a track in through the drive's own registers, waits through the ROM's WakeUp for the head to settle, and expects sector 2 to be the next to come round. That holds only if the step and the settling took between about 37 and 50 ms, as on a Mac. |
| The Surgeon | Runs, with `--app Surgeon --host screen --size 512x342`, from its MOOF image, past the routine that decrypts its code in trace mode, to the check of its master disk, where it asks for the Master Disk. The check times eight marks on track 4 with the VIA's timer against a table of what they took on its own disk, each to within about ten ticks. On the image's track, the seventh mark comes about nine bits late against the sixth and the eighth. A bitstream keeps a track's bits but not how long each took, so it can't show a stretch the duplicator wrote faster or slower than the rest. archive.org's note on the image says the game fails its check in archive.org's in-browser emulator too, and passes in MAME run locally. What MAME does differently isn't known yet. |

When you try another, the list of missing calls it prints when it quits
(see [When it stops](#when-it-stops)) is what to report.


## Getting the application to macrt

A Mac file has two forks. The **data fork** is what every other computer
sees. The **resource fork** holds almost everything that makes an
application: its code (`CODE` resources), menus, windows, dialogs, icons
and strings.

If you copy a Mac application to a PC or a Unix machine the ordinary way,
or download it from somewhere that wasn't careful, the resource fork is
lost. What's left is a file of zero bytes, or a little data, that nothing
can run.

So the application has to arrive in a form that carries both forks. macrt
reads all the common ones, and you don't have to say which one you have;
it works it out from the contents:

| Form | Looks like | Where it comes from |
| --- | --- | --- |
| MacBinary I, II or III | `App.bin` | downloads; most Mac transfer programs could write it |
| BinHex 4.0 | `App.hqx` | downloads, Info-Mac and Usenet archives |
| AppleSingle | one file holding both forks | A/UX, some archives |
| AppleDouble | `App` next to `._App`, `.AppleDouble/App` or `App.rsrc` | `unar`, netatalk, macOS copying to a FAT disk |
| Basilisk II and SheepShaver shared folders | `App` next to `.rsrc/App` and `.finf/App` | the emulators' host folder |
| Executor | `App` next to `%App` | Executor's host folder |
| a resource fork on its own | `App.rsrc`, or any file that is one | `DeRez`/`Rez` tooling, resource editors |
| a macOS file | `App` itself, through `App/..namedfork/rsrc` | a Mac running macOS |
| an HFS or MFS disk image | `Disk.dsk`, `.img`, `.image`, `.hfs`, `.toast` | raw and DiskCopy 4.2 floppy images (800K and 1.4MB HFS, 400K MFS), with each sector's 12 tag bytes or without them, HFS CD images, partitioned hard disk images |
| a MOOF floppy image | `Disk.moof` | Applesauce, imaging a 400K or 800K floppy's tracks bit by bit |

For AppleDouble and the emulator folders, give macrt the data-fork file
(`App`), and keep the side file where it is.

### Checking a file

```sh
python -m macrt Game.bin --list
```

`--list` prints the file's type and creator and every resource it holds,
then exits. For menus, windows, dialogs, `SIZE` and `vers` resources it
also says what they contain. It's the quickest way to check that an
application arrived with its resource fork: a `CODE` line means there is
68k code to run.

```
$ python -m macrt tests/apps/Hello.bin --list
Hello: application, type 'APPL', creator 'HELO'
  data fork 0 bytes, resource fork 1,007 bytes, 6 resources
  CODE      0       24  purgeable,locked,preload '' jump table, 1 entries
  CODE      1      460  purgeable          'Main'
  MENU    128       40                     Apple: About Hello…, -
  MENU    129       29                     File: Quit
  WIND    128       24                     'Hello', 260x160, procID 0
  SIZE     -1       10                     256K preferred, 128K minimum
```

Each line gives the resource's type, ID, size in bytes, attributes and
name, then what it holds.

If macrt says the file *has no resource fork*, the fork was lost on the way.
Go back to the archive or disk image it came from.

### Archives

Most Mac software online is in an archive: `.sit` (StuffIt), `.cpt`
(Compact Pro), `.sea` (self-extracting), `.sit.hqx`, or a `.zip` with a
`__MACOSX` folder. macrt doesn't unpack archives; it tells you when you've
given it one. Unpack it with The Unarchiver, which keeps the resource
forks:

```sh
unar Game.sit.hqx               # decodes the BinHex and unstuffs what's inside
python -m macrt "Game Folder/Game"
```

On Linux and Windows, `unar` writes each resource fork, with the file's
type and creator, as an AppleDouble file named `App.rsrc` next to `App`.
An application's data fork is usually empty, and then `unar` writes only
`App.rsrc`. Give macrt either `App` or `App.rsrc`; both find the same
file. On macOS, `unar` writes real resource forks.

Don't use `unzip` on a zip from a Mac: it leaves the resource forks in the
`__MACOSX` folder, away from their files. `unar` puts them back together.

For example, Vector 2.5, a small program from the Info-Mac archive:

```sh
$ curl -O https://ftp.funet.fi/pub/mac/info-mac/game/arc/vector-25.hqx
$ python -m macrt vector-25.hqx
macrt: vector-25.hqx is a StuffIt archive: unpack it first, for example with The Unarchiver (unar vector-25.hqx)
$ unar vector-25.hqx
$ ls "vect_250 folder"
Vector 2.5 Doc  Vector 2.5.rsrc
$ python -m macrt "vect_250 folder/Vector 2.5"
```

### Disk images

Many collections come as disk images: floppies, CD-ROMs and hard disks.
macrt reads HFS and MFS volumes directly, so there's nothing to extract:

```sh
python -m macrt "System 6.0.8.dsk"                  # a disk with one application
python -m macrt "System 6.0.8.dsk" --app TeachText
python -m macrt Tools.img --app "Tools:Disk Tools:Disk First Aid"
python -m macrt Paint.dsk                           # a 400K MFS floppy from 1984
```

`--app` takes the application's name, or its Mac path with folders
separated by colons, starting from the disk's name or from any folder
below it. Names are matched without regard to case. Run a disk that holds
several applications without `--app`, and macrt lists them:

```
$ python -m macrt mac6-0-8.dsk
macrt: mac6-0-8.dsk holds 7 applications; pick one with --app:
  Mac System 6.0.8:Applications:Microsoft Excel:Microsoft Excel
  Mac System 6.0.8:Games:Creepy Castle:Creepy Castle™
  Mac System 6.0.8:Games:Operation Tsunami:Operation Tsuanmi
  Mac System 6.0.8:LabelWriter II Folder:LabelWriter II App
  Mac System 6.0.8:LabelWriter II Folder:TeachText
  Mac System 6.0.8:System Folder:PrintMonitor
  Mac System 6.0.8:TeachText
```

When two applications have the same name, as the two TeachTexts do here,
`--app TeachText` takes the one nearest the top of the disk
(`Mac System 6.0.8:TeachText`). Give more of the path to get the other:
`--app "LabelWriter II Folder:TeachText"`.

macrt reads raw images and DiskCopy 4.2 images, including images of
partitioned disks (an Apple partition map with an HFS partition).

It also reads Applesauce's MOOF images of 400K and 800K floppies. These
keep each track as the bits the drive reads, as the moof-a-day
collection keeps its floppies; macrt decodes them back to sectors, the
way a Mac's floppy drive does, when it opens one. A sector that can't be
read is left as zeros, and a program that reads it through the floppy disk
driver is told it can't be read, as on a Mac. A few keep a track instead
as the flux changes Applesauce timed going by, where bits can't say what's
there; macrt reads those as a Mac's floppy controller would. MOOF images
of 1.4MB floppies aren't read: convert them to a raw image first.

MFS disks are the 400K floppies of System 1 to 4, from 1984 to 1987: the
first MacPaint and MacWrite disks, and most early games, came on them.
MFS has no folders of its own. The folders you saw in the Finder were kept
in its Desktop file, and applications never saw them, so macrt puts every
file at the top of the disk and `--app` takes just the name. An
application on an MFS disk sees an ordinary HFS volume, as it would have
on a Mac Plus.

A few early games took the Finder's place on their disk, so that a Mac
started from it went straight into the game. Given a disk without
`--app`, macrt runs the program the disk starts up into, as its boot
blocks name it, when that isn't Apple's Finder, and otherwise the disk's
only application.

Some early programs check that they're on their original floppy, by
reading marks a copier can't copy. From a MOOF image, which keeps what's
on each track, a check that reads through the floppy disk driver gets
what's on the original, and so does one that puts its own routine in the
driver's place to read past a sector's address field, as Excel 1.0 does,
or to give the ROM's routine marks of its own to look for, as Dollars and
$ense and PFS:FILE do. A check that times the disk as it turns, through the ROM's
routines that find each sector's address field as it comes round and
step the head, as Mac Attack's does, sees the original's timing: the
disk turns at the speed its zone of tracks did. Where a track goes on long with no flux change,
the drive reads noise there, different bits each time, and a check that
reads it twice, as Q-Sheet's does, sees that it's the original; a few zeros
together the drive reads as written, so a check that counts a turn of a
track whose bytes are further apart than a disk's own, as The Ancient Art
of War's does, counts what the original gives. A check
that steps the head itself and counts the bytes the floppy controller
reads from the track it moved to, as PageMaker's does a track in from
its key sector, sees them go by as the disk turns, and one that times
them with the VIA's timer, as Jazz's does, counts what the original
gives: the VIA and the drives keep one clock. A check whose own routine
reads a sector bit by bit as the floppy controller shifts them in, as
Shufflepuck Cafe's does, sees the original's bits too. It stops the
shifting for as long as its own instructions take, and macrt counts what
each instruction would take a Mac, so the same bits go by unread as on
the original. A check with its own routine to find a sector's address
field, as Dragonworld's has, is called for each sector the driver
reads. A key track whose flux changes come a bit cell and a half apart,
as Fraction Action's, MathTalk's and KidTalk's do, reads differently
each time, as on the original: the floppy controller reads each such
change as one cell after the last or two, as it comes a little early or
late, and their check reads the track twice and sees the difference that
a copy, written as bits, can't have. A check that steps the head through
the drive's registers, waits through the ROM's WakeUp and expects a
given sector next, as Moebius's does, sees the skew from one track to
the next that the original has: the drive says at once that a step is
over, and the disk driver's step and settle times, which WakeUp waits
for, are 12 and 30 ms. Those are macrt's choice, within what such a check
allows, not times known from the ROM. Other checks that
drive the floppy drive's hardware themselves still fail: the program
shows its own "damaged disk" or "not an original" alert, or asks for its
master disk.

macrt can't read compressed images yet: NDIF (DiskCopy 6) and UDIF
(`.dmg`, `.smi`). Convert them to a raw image first.

### Where to find software

Use software you have the right to run. Apple's own system disks, such as
System 6.0.8, are a good first test: they include TeachText and a few
small utilities. The Info-Mac archive, mirrored for example at
`https://ftp.funet.fi/pub/mac/info-mac/`, holds thousands of freeware and
shareware programs as `.hqx` files: unpack them with `unar` as above.
Small black-and-white programs are the likeliest to run. The Internet
Archive keeps images of many original 400K floppies, as raw `.dsk` or
DiskCopy images that macrt opens as they are, and as MOOF images in its
moof-a-day collection, about 150 early Mac titles.


## Running it

```sh
python -m macrt FILE... [--app NAME]... [options]
```

| Option | What it does |
| --- | --- |
| `--app NAME` | Picks the application on a disk image, by name or Mac path. Repeat it to run several. |
| `--list` | Lists the file's resources and exits. |
| `--view` | Shows the application's menus, windows and dialogs from its resources, without running its code. Useful when the code won't run. |
| `--trace-traps` | Logs every Toolbox and OS trap the application calls. |
| `--system FILE` | Uses a real System file, or a disk image holding one, in place of macrt's own small one. See below. |
| `--host desktop` | Each Mac window is a window on your desktop, with one menu bar for the front application. This is the default. |
| `--host screen` | Everything is drawn inside one window that is the Mac's screen. |
| `--menubar global` | One menu bar, the front application's. This is the default. |
| `--menubar window` | A menu bar under each window's title bar. |
| `--size WxH` | The Mac screen's size, such as `512x342` or `640x480`. The default is your screen's size with `--host desktop`, and 640×480 with `--host screen`. |
| `--speed N` | Runs 68k code at about N times a Mac Plus's speed, by the cycles its instructions would take a 68000. Games from 1984 time themselves by how fast the processor goes, and are unplayable at full speed; `--speed 1` is a Mac Plus. The Toolbox's routines, which macrt runs itself, aren't slowed, and above about 3 the counting can't keep up. The default is as fast as it goes. |
| `--no-sound` | Keeps the Mac's sound off your computer's speakers. See [Sound](#sound). |
| `--record-sound FILE.wav` | Writes the Mac's sound to a WAV file as it plays, as well. |
| `--model plus` | Says the Mac is a Mac Plus, with a 68000 and no floating-point unit. This is the default. Programs from before the Mac II need it: a 68000's exception frames are smaller than a 68020's, and MacWrite 4.5 pops its own. |
| `--model ii` | Says the Mac is a Mac II, with a 68020 and a 68881, for a program that needs one. The instructions run are the same either way. Some games pick how to draw by the processor: on a 68000 they draw straight into the screen's memory as if it were a 512×342 Mac's, which on a bigger screen puts their picture in the wrong place. Give them `--model ii`, or `--size 512x342`. |
| `--classic-menus` | Menus stay open only while the mouse button is held, as before System 7. |
| `--rainbow` | Shows the striped Apple logo in the menu bar. |

Quit an application from its own File menu, or close macrt. Ctrl+C in the
terminal quits macrt and everything running in it.

On a PC keyboard, **Ctrl** is the Mac's Command key (⌘), **Alt** is Option,
and the Windows or Super key is Control. On a Mac keyboard, the keys are
the Mac's own.

The keys have the key codes of the modelled Mac's keyboard. A Mac Plus's
arrow keys are $46, $42, $4D and $48, the codes its keypad's +, *, / and =
send with Shift; a Mac II's ADB keyboard has $7B to $7E for them. The
keypad's keys have codes of their own, apart from the same digits on the
main row, for games that steer by the keypad. While an arrow key is down,
it is in the `KeyMap` by both keyboards' codes, since games written for
the one keyboard read the other's.

### Several applications at once

```sh
python -m macrt Hello.bin Dialogs.bin
python -m macrt mac6-0-8.dsk --app TeachText --app "Creepy Castle™"
```

Every file you name is launched, in order. When disk images are among
them, each `--app` is looked for on the images in turn and taken from the
first one that has it. Without `--app`, each image gives its only
application.

An image nothing is launched from, one with no application on it or, with
`--app`, none of the names, is a disk of documents. Every image is in a
drive from the start, the first in the internal drive, so a game on two
floppies finds its second disk when it asks for it by name, or puts it in
through a volume control block of its own, as Uninvited does:

```sh
python -m macrt "BDC 1.moof" "BDC 2.moof" --app "Beyond Dark Castle" --host screen
```

A program that ejects a floppy gets it back a second later, as if you had
put it back in. The drive then posts a disk-inserted event, and the disk
is mounted again. A game that ejects its disk and waits for a disk, as
Seven Cities of Gold does for its map disk, goes on with the same disk.

A game on more disks than there are drives ejects a disk, unmounts it
(`_UnmountVol`) and asks for another. Then a dialog asks which of the
disk images you named goes in that drive, the next one in turn chosen.
Insert puts in the disk you choose, and Put Back puts back the disk that
came out. If the disk you choose is in another drive, it comes out of
that drive, and the disk it replaces goes in there, unmounted. Without a
display to ask on, the next disk in turn goes in. Wizardry VI is played
this way:

```sh
python -m macrt A.moof B.moof C.moof D.moof E.moof F.moof --app Bane --host screen
```

The applications run side by side, as under MultiFinder. Each has its own
memory partition, A5 world, low-memory globals and menu bar. The menu bar
belongs to the front application, and clicking another application's
window brings that application to the front.

An application that launches another one (with `_Launch`) gets it the same
way. A `_Launch` without `launchContinue` replaces the application, as on a
Mac.

### Documents and saving

An application starts in its own folder, as when the Finder opens it, and
its Open and Save dialogs start there too. These are macrt's own dialogs,
not the application's. Where a file it saves ends up depends on where the
application came from:

| The application came from | What happens to files it writes |
| --- | --- |
| a folder on your computer | They're written into that folder. A file read as MacBinary or AppleSingle is written back the same way. Any other file is written as its data fork under its own name. Its resource fork, type and creator go in the side file it already had, such as `unar`'s `name.rsrc`, or for a new file in an AppleDouble `._name`. macrt reads all of these back the next time. |
| a disk image | The changes are kept in memory and lost when macrt quits. The image file itself is never changed. |

A BinHex file is text, not a Mac file, so macrt treats it as locked. Some
applications keep their settings in their own resource fork, and can't
save them while they run from a `.hqx`. Unpack it with `unar` first.

Applications also see a startup disk called **Macintosh HD** with a System
Folder. Preferences files they write there last only until macrt quits.

### Printing

macrt's printer prints to a PDF. Page Setup and Print show macrt's own
dialogs rather than a printer driver's:

- **Page Setup** picks the paper (US Letter, US Legal or A4) and whether
  the page is tall or wide.
- **Print** picks all the pages or a range of them, the number of copies,
  and the PDF to save. It's named after the front window, in the folder
  you started macrt from (`Untitled.pdf`, or `Untitled 2.pdf` if that's
  taken). Choose… picks another place.

When the PDF is written, macrt says so:

```
macrt: printed 2 pages to /home/you/Untitled.pdf
```

The printer says it's an ImageWriter that prints at 72 dots per inch, so
every application draws its pages with QuickDraw, as it does on the screen.
The PDF's pages are black and white at 288 dots per inch. Applications from
1984 find the printer through their own glue code, as they did on a Mac
(see [What works](#what-works)). MacPaint prints a bitmap at a time,
straight to the printer driver, and shows no Print dialog. Its PDF gets the
name the dialog would have given it. The printer is the same with a real
System file (`--system`), whose own printer drivers would drive a serial
port.

### A real System file

Without `--system`, applications run with macrt's own small System file.
It has what applications most often ask a System file for: the standard
window, menu and control definitions, the packages (`PACK` 0 to 15), the
stop, note and caution icons (`ICON` 0 to 2), the standard cursors
(`CURS` 1 to 4: I-beam, cross, plus and watch) and the US international
resources (`INTL` 0 and 1). It also has the pattern list (`PAT#` 0) that
paint programs show as their palette, and the desktop's gray and light
gray (`PAT ` 16 and 17). Its icons, cursors and patterns are
drawn for macrt. Some applications look for resources that only a real
System file has, such as other icons, strings (`STR#`), the fonts
themselves and the Script Manager's `itl` resources.

When the applications come from a disk image that has a System file of
its own, macrt's System gets that file's fonts of the families it hasn't
got, up to 128K of them: programs drew in the fonts of the System they
started up from, and some brought their own there, as Mac Attack's tanks
and landscape are characters in fonts of its disk's System. Fonts of the
standard families stay macrt's, but the monospaced ones, Monaco and
Courier, are measured with the widths of that System's strikes of them,
as with a real System file (below): Hacker types each letter in Monaco 9
and its cursor after it, then backs up six pixels, Monaco's width, to
type the next letter over the cursor.

For everything else, give them a real System file:

```sh
python -m macrt Game.bin --system "System 6.0.8.dsk"
python -m macrt Game.bin --system "System Folder/System"
```

The System file's resources then sit at the bottom of every
application's resource chain, as on a Mac, and its desk accessories are
in the Apple menu. Chicago, Geneva and the other standard fonts are
still drawn with your computer's fonts, but measured with the widths of
the System file's own bitmap fonts, at the sizes it has them, so that
text lines up as it did on a Mac. (A standard font the application
carries in its own file, at the size asked for, is drawn from its
bitmap, as Balance of Power's New York is, and Gemstone Warrior's
Chicago, which it makes the application font by setting `ApFontID`. An
application's own Chicago 12 is the one its menus are drawn in, as on a
Mac: The Fool's Errand's menu titles are Roman numerals in its font.)
Its 68k definition procedures and packages aren't run: windows, menus and
the standard controls are always drawn by macrt, and the package calls
are always macrt's. Anything above
that the real System file lacks, macrt adds.

### Games from 1984

The first Macintosh games often go around the Toolbox. They draw
straight into the screen, read the mouse button from the hardware, and
keep time by the processor's speed or by the VIA's timers. macrt gives
them what they reach for:

- **The screen buffer.** The bitmap at `ScrnBase` mirrors the windows.
  What QuickDraw draws is written there, and what a program writes there
  shows in the window it lands on, at its next trap or within a tick if
  it calls none. What a program writes where no window is shows on the
  desk, which only `--host screen` has.
- **A Mac 512K's screen.** With `--size 512x342` the screen buffer is at
  `$7A700`, where a 512K or a Plus has it, for games that write there
  without asking `ScrnBase`. Most games from 1984 and 1985 expect that
  screen. Its alternate buffer is `$8000` below, at `$72700`. An
  application launched with `Launch`'s page option finds it in
  `CurPageOption`, and while the VIA's `vPage2` bit is clear the desk and
  the menu bar show the alternate buffer rather than the main one. The
  windows go on showing what's drawn in them.
- **The VIA**, at `$EFE1FE`: the mouse button, the vertical blanking flag
  at each tick, the one-second flag, the two timers, and which screen
  buffer is shown. Above it, where a Plus looks for a test ROM, there is
  none, and reading there is no bus error.
- **The exception vectors.** What the System leaves in them is in a page
  of ROM over the top of RAM, at `ROMBase`, as on a Mac. A program can put
  its own handler in any of them, for a `TRAP`, dividing by zero or `CHK`,
  and it comes back past the instruction that raised it. A handler for the
  VIA's interrupts, at `$64`, is interrupted at each tick and once a
  second, from the next tick on; it goes on to the ROM's, as
  Flight Simulator's does, to clear them. When the program quits, the
  vectors it took are the System's again. The A-line vector, `$28`, holds
  the ROM's trap dispatcher, and the start-up code that puts it there is
  in the ROM too, `LEA` then `MOVE.L A0,$28`, for programs such as Lotus
  Jazz that look for it there to put it back; a jump to the dispatcher
  with a trap's address stacked does the trap.
- **The stack.** An application gets 32K of stack between `ApplLimit` and
  `CurStackBase`, and `DefltStack` says so: The Fool's Errand takes what
  that falls short of 32K from its heap. `ReserveMem` grows the heap
  toward `ApplLimit` for what it reserves, and all the way when that's not
  enough, as a 64K ROM's does for programs that ask for more than there is
  in place of `MaxApplZone`.
- **The drives.** A floppy disk's image is in drive 1, the internal
  drive, with the floppy disk driver's reference number, -5. macrt's own
  disk is a hard disk, from drive 3. Flight Simulator looks for its disk
  in drive 1. Each drive is in the drive queue at `DrvQHdr`, with the
  flags before its element: a floppy drive's disk in place, a hard
  disk's 8 (it can't be ejected).
- **The floppy disk driver**, `.Sony`. `Read` and `Write` with reference
  number -5 read and write the disk in drive 1 or 2 by sector, as copy
  protection and disk tools do, with each sector's 12-byte tag at
  `TagData` and in the buffer `Control` 8 gives it; `Status` 8 is the
  drive's. What's written stays in memory, as with the File Manager's
  writes to a disk image. A program can put its own routine in the 64K
  ROM's `RdData` vector to read what's on the disk after a sector's
  address field; macrt calls it with a MOOF image's bytes there coming in
  through the IWM, the floppy controller, at `$DFE1FF`, and with A3
  pointing at the ROM's table of the six bits each byte from the disk
  stands for (indexed by the byte, $FF for one that stands for none), as
  MacGammon's routine expects. The routine can
  go on to the ROM's, which `RdData` held, with A1 pointing at the five
  marks the data field begins and ends with: it reads the field into the
  buffer at A0, or compares it with the buffer while `DskVerify` is set.
- **What the Event Manager keeps in low memory:** the mouse (`Mouse` and
  `RawMouse`; and `MTemp`, which counts its movement up, for a program
  that clears it to see how far it has moved) and its button (`MBState`),
  the keys held down (`KeyMap`, which `GetKeys` reads; an arrow key by
  both a Mac Plus's code and ADB's) and the last key
  pressed (`KeyLast`, `KeyTime`). They follow the mouse with the button up
  too. A program can move the mouse itself, writing `RawMouse` and setting
  `CrsrNew`, and macrt's cursor task copies it to `Mouse` at the next tick
  and clears `CrsrNew`, which Pirates! waits for. While the program has
  the cursor hidden, the mouse stays where it put it and goes on from
  there as the host's pointer moves, which is how Crystal Quest steers;
  with the cursor showing, it's back under the pointer when that moves.
- **`--speed`**, for games that run as fast as the processor goes. A
  modern computer runs them hundreds of times too fast; `--speed 1` is
  about a Mac Plus. Each instruction is counted by the cycles it would
  take a 68000, and the 68k code is held to the 6 million a second a
  Plus's gets through, its RAM shared with the video and the sound. A
  game that times its frames by the ticks, as Downhill Racer does, sees
  them take as long as on a Plus. `TickCount` is what `Ticks` holds, as
  the ROM's is, and `Ticks` changes at each tick, so a program that reads
  one and then the other sees the same count.

So a game on a 400K floppy runs like this:

```sh
python -m macrt "Mac 128K Games Vol. 1/Frogger.DSK" --host screen --size 512x342 --speed 1
```

A game that doesn't use menus may draw over the menu bar, in the top 20
rows of the screen. As on a Mac, the bar is in the screen buffer, so what
the game draws there, white or black, shows over the bar until it calls
`DrawMenuBar`. A window that reaches up over the bar doesn't draw there,
as its visRgn leaves out the bar, as the desk (`GrayRgn`) does. A game
that means its window to cover the bar puts the bar back in the visRgn
itself, as Indiana Jones and the Last Crusade does, and then what it
draws there shows over the bar in the same way. A game's windows go on
showing the main screen buffer while the VIA shows the alternate one.

### Sound

A Mac 128K, 512K or Plus makes its sound from a buffer in memory, at
`SoundBase`: at each tick, 370 samples from it go to the speaker, while
the VIA has the speaker on, at the volume in the VIA's low bits. macrt
plays the buffer the same way, through your computer's sound output.
The volume starts at 7, the loudest, in the VIA, in `SdVolume` and in
parameter RAM. What's in the buffer comes from three places:

- **The Sound Driver**, `.Sound`, reference number -4. `StartSound`, a
  `Write` to it, plays a sound through one of its three synthesizers:
  square-wave, tones of a pitch, a loudness and a length; four-tone,
  four waveforms played together, as MusicWorks plays its songs; and
  free-form, samples at a rate. An asynchronous sound plays while the
  program goes on, those after it waiting their turn, and its completion
  routine is called when it's done. `StopSound`, a `KillIO`, stops them.
- **The Sound Manager** (Inside Macintosh V), which System 6 and 7
  programs call: `SndPlay` plays a `'snd '` resource, and sound channels
  take commands from the program, `SndDoCommand` queueing them and
  `SndDoImmediate` sending them at once. At each tick, each channel's
  synthesizer makes the next frame, and they're added to the Sound
  Driver's. The note synthesizer plays notes, from a sine to a square
  wave as the timbre goes up; the wave table synthesizer plays the
  program's own waveform; the sampled sound synthesizer plays sound
  headers' samples (`bufferCmd`), and notes on an instrument from one
  (`soundCmd`), looped while the note lasts. `waitCmd`, `callBackCmd`,
  `pauseCmd`, `ampCmd`, `rateCmd` and the rest are there, and so are
  modifiers, the program's own, from `SndAddModifier`. `SndPlay` with no
  channel, or not asynchronously, waits for its sound to end, as does
  `SndDisposeChannel` without `quietNow`, with the ticks going on.
  MineSweeper, Glider, Glypha, Shadow Keep and Tesserae play their sounds
  this way.
- **The program itself**, writing its samples into the buffer at each
  tick, as Dark Castle, Crystal Quest and SmoothTalker's speech do.

`--no-sound` keeps the sound off your speakers, and `--record-sound
FILE.wav` writes it to a WAV file as well: 8-bit mono, 22,200 samples a
second.

How it differs from a Mac:

- macrt's ticks are 60 a second, a Mac's 60.15, so the samples play at
  22,200 a second rather than 22,255, and the Sound Driver's tones are 4
  cents flat.
- The ticks, and so the sound, go on while the application's code runs
  and while it waits: in `Delay`, in `WaitNextEvent` with time to sleep,
  in a menu, a modal dialog or a window being dragged. Its VBL tasks and
  Time Manager tasks run at those ticks too, but the VIA's interrupts
  wait for the program's own code to run again, since they would come in
  the middle of the call it waits in. Under MultiFinder, the ticks that
  run are those of the application that has the processor, or had it
  last, each application's VBL tasks being its own.
- The alternate sound buffer isn't played, and timer 1 doesn't turn the
  speaker on and off by itself, as some programs have it do through the
  VIA.
- `SysBeep` is your computer's own beep. MIDI, which goes out through
  the serial ports, plays nothing.
- The Sound Manager's synthesizers stand in for Apple's `'snth'`
  resources, which aren't run, and nor are modifiers that are resources.
  Its sounds are the Plus's, in one channel, so stereo's sides are added
  together; compressed sounds are silent, and `syncCmd` doesn't wait for
  other channels. A channel's commands past its full queue wait outside
  it, so a program never waits for room.


## What happens at launch

1. **Memory.** macrt reads the application's `SIZE` resource (or, without
   one, the sizes from the Finder's Get Info) and gives it a partition of
   that size in an 8 MB Mac. An application that asks for more than is
   free is refused, and macrt says how much it needs.
2. **The A5 world.** `CODE 0` describes the application's globals, which
   sit below A5, and the jump table, which sits above it. macrt builds them
   as the Segment Loader did and loads `CODE 1`. The other segments load
   when the code first calls into them.
3. **The processor.** unicorn's 68020, in 24-bit mode, starts at the
   first jump-table entry. Gestalt, `SysEnvirons` and the low-memory
   globals say it's a Mac Plus, with a 68000 and no floating-point unit,
   and the exception frames it stacks are a 68000's (see `--model`).
4. **Traps.** Every A-line trap the code executes (`_NewWindow`,
   `_DrawString`, `_WaitNextEvent` and so on) stops the processor. The
   matching Python routine then reads its arguments from the 68k stack or
   registers and does what Inside Macintosh says that routine does.
   - Windows and menus become Qt windows and menus.
   - QuickDraw draws into a 1-bit bitmap for each window, which Qt shows.
   - The Mac's data structures (window records, handles, regions and so
     on) are real, in the Mac's memory, so code that reads them directly
     still works.
5. **Events.** Mouse clicks, keys, and update and activate events come from
   Qt and are queued for `GetNextEvent` and `WaitNextEvent`. An application
   waiting in `WaitNextEvent` lets the others run. One whose `SIZE`
   resource says it takes high-level events finds an Open Application
   Apple event waiting, as System 7's Finder sends when it opens an
   application without documents.


## What works

There are more than 700 Toolbox and Operating System routines:

- **Memory Manager:** handles, pointers, heap zones, purging, the system
  heap, and MultiFinder's temporary memory. A block that grows, with
  `SetPtrSize` or `SetHandleSize`, grows where it is when the relocatable
  blocks after it can be moved or purged out of the way, and blocks are a
  multiple of 2 bytes long on a 68000 and of 4 on later processors, as
  the ROM makes them.
- **Segment Loader:** `LoadSeg`, `UnloadSeg` (given an address in the
  segment or one of its jump-table entries, as Pascal's `@` gives; an
  entry may be called through an address with flags in its top byte,
  which a 68000 ignores),
  `GetAppParms`, `ExitToShell` and `_Launch`.
- **Resource Manager:** the resource chain, from the application down to
  the System file; `OpenResFile` for files next to the application;
  adding, changing and writing resources; and an application's own patch
  on `_ResourceStub`, through which it reads resources in, as some
  installers, and applications compressed by AutoDoubler, do to decompress
  theirs. A resource that isn't found is
  `resNotFound` in `ResError` only when the last file looked in (the
  System file, or the current one for `Get1Resource`) has its type, as
  Microsoft File counts on.
- **QuickDraw**, in black and white:
  - ports, regions, patterns, polygons, every shape, text, measured
    by `MeasureText` too, and `CopyBits`;
  - a port's own `textProc` and `txMeasProc`, which draw and measure
    its text in place of `StdText` and `StdTxMeas`;
  - pictures, played and recorded;
  - cursors, `CopyMask` and `SeedFill`;
  - `Random`, from `randSeed`; a program can seed it from `RndSeed`,
    in low memory, which is different each time macrt starts;
  - colour calls, mapped to black and white.
- **Window Manager:** the standard window kinds, with dragging, growing,
  zooming and update regions; between `BeginUpdate` and `EndUpdate` only
  what's in the update region is drawn. A window with the application's
  own `WDEF`, a resource or code it put in `windowDefProc` itself, is that
  code's to shape and draw: the window takes the shape of the structure
  region it works out, shows the frame it draws, and its `wHit` answer
  says where a click is. An application's `WDEF` that passes messages to
  the System's `WDEF` 0 or 1, as Excel's do, gets macrt's frames.
- **Menu Manager:** `MENU` and `MBAR` resources, key equivalents, marks,
  styles and `AppendResMenu`.
- **Event Manager:** `GetNextEvent`, `WaitNextEvent`, `EventAvail`,
  `Button`, `GetKeys`, `PostEvent` and `FlushEvents`. `GetNextEvent` with
  nothing to give returns at once, unless it's asked again in the same
  tick: then it waits for the next, so that a program's event loop
  leaves the host the processor. `KeyTranslate` uses
  the US layout whatever `KCHR` it's given.
- **Control Manager:** buttons, check boxes, radio buttons and scroll bars,
  plus the application's own 68k `CDEF`s, including code it puts in a
  control record's `contrlDefProc` itself.
- **Dialog Manager:** modal and modeless dialogs, alerts, `ParamText`,
  filter procedures and the item calls, with edit fields edited through
  TextEdit.
- **List Manager:** lists drawn by `LDEF` 0 or the application's own
  `LDEF`, with clicks, Shift and Command to extend the selection,
  dragging, autoscrolling, scroll bars and double clicks. Cells are kept
  where the ROM keeps them, so code that reads a list's record and its
  data directly sees what it expects.
- **TextEdit:** editing, selection, line breaking, scrolling, styled text
  and `TETextBox`; and the routine at `TEDoText`, for programs that find,
  highlight, draw and place the caret through it themselves. A record's
  `highHook` and `caretHook` are called as TextEdit's own, `_InvertRect`,
  is: the rectangle's address on the stack, for the hook to pop.
- **Scrap Manager:** the desk scrap. Text in it is shared with your
  computer's clipboard, both ways. `UnloadScrap` writes it to the
  Clipboard File in the System Folder, as a Mac does.
- **File Manager:** files, folders and volumes, the HFS catalog calls,
  working directories, the `FSSpec` calls and `FindFolder`. See
  [Documents and saving](#documents-and-saving).
- **Device Manager:** an application's own drivers, the `DRVR` resources
  in its file. `Open` finds one by name and puts it in the unit table, and
  `Read`, `Write`, `Control`, `Status`, `KillIO` and `Close` call its
  routines, synchronously, asynchronously or immediately; the driver
  finishes each request through `IODone`, which calls the request's
  completion routine. The System file's drivers, other than macrt's own
  Sound Driver, `.Print` and `.Sony` (see [Games from
  1984](#games-from-1984)), aren't run, except as desk accessories.
- **Desk Manager:** desk accessories, the `DRVR`s an application lists
  in its Apple menu with `AddResMenu`, from the System file (`--system`)
  or its own file. `OpenDeskAcc` opens one for the application that
  picks it, as a driver. Its window then takes clicks through
  `SystemClick`, keys and updates through `SystemEvent`, which
  `GetNextEvent` and `WaitNextEvent` call, and the Edit menu through
  `SystemEdit`; `SystemTask` gives it time, and a pick from its own menu
  goes to it. It closes from its close box, with `CloseDeskAcc`, or when
  the application quits. macrt's own System file has no desk
  accessories. `AddResMenu` lists them in alphabetical order, as a Mac
  Plus does (Inside Macintosh IV-55); a 128K or 512K Mac listed them
  in the System file's order, so a game that opens a desk accessory by
  its place in the menu may open another one, as Fokker Triplane's
  Help does.
- **Alias Manager:** alias records in System 7's format, found again
  after their file or folder has been moved or renamed, and brought up to
  date; `MatchAlias` and `GetAliasInfo`; `ResolveAliasFile`, which follows
  the Finder's alias files; and Apple event coercions between an alias
  and an `FSSpec`. There are no file servers, so an alias whose disk
  isn't there doesn't resolve.
- **Standard File:** the Open and Save dialogs, filtered by file type and
  by the application's own filter procedure. A custom dialog's own items
  (its buttons, check boxes, radio buttons, text and edit text) are shown
  under the standard ones, and its `dlgHook` is called as each is clicked
  or typed in; MacWrite's choice of formats in its Save dialog is one, and
  Microsoft File's name for a new datafile another.
- **Icons:** `ICON`, `SICN`, `cicn` and icon families, through the mask.
- **Process Manager and MultiFinder:** the process calls, temporary memory
  and `SetFrontProcess`.
- **Apple Event Manager:** descriptors, lists, records, and Apple events
  with their parameters and attributes; the handler tables; and
  coercions between numbers, text and four-character codes, plus the
  application's own coercion handlers. An application gets the Open
  Application event at launch (see [What happens at
  launch](#what-happens-at-launch)), can look through what's waiting with
  `GetSpecificHighLevelEvent`, and can send events to itself, which is
  how recordable applications carry out their own commands.
- **PPC Toolbox:** `PPCInit`, and ports opened and closed, as System 7's
  applications do at startup, though nothing can connect to them. The
  Process Manager names each application's own port.
- **SANE:** `FP68K`'s arithmetic, comparisons, conversions and
  environment calls, correctly rounded in every rounding direction and
  precision, with the Apple Numerics Manual's exceptions, NaN codes and
  halts; `Elems68K`'s logarithms, exponentials, powers, financial
  functions, trigonometry and random numbers; and `PStr2Dec`, `CStr2Dec`
  and `Dec2Str`.
- **International Utilities:** dates and times written out as the `INTL`
  resources say, `IUMetric`, `IUGetIntl`, `IUSetIntl`, and string
  comparison and equality.
- **Script Manager:** the Roman script only. Character types, upper and
  lower case and accents (`Transliterate`, `UppercaseText` and the like),
  words (`FindWord`), line breaks (`StyledLineBreak`), `TruncString`,
  justified text (`MeasureJust`, `DrawJust`, `Pixel2Char`), and the
  Script Manager's variables.
- **Other managers:** Gestalt, `SysEnvirons`, the Notification Manager,
  the date and time, `SysBeep`, VBL tasks (`VInstall` and `VRemove`),
  run once a tick while their application runs or waits, and the Time
  Manager (`InsTime`, `PrimeTime` and `RmvTime`); `HWPriv`'s
  cache calls, with no caches to flush. The Package
  Manager's `InitPack`, `InitAllPacks`, `DILoad` and `DIUnload` have
  nothing to do, every package being always there.
- **The hardware**, for programs from the Mac's first years: the screen
  buffer at `ScrnBase`, the VIA, and the Event Manager's low-memory
  record of the mouse and keyboard (see [Games from
  1984](#games-from-1984)).
- **Printing Manager:** the calls of System 6 and 7's `PrGlue`, and the
  printer resource file and `.Print` driver that earlier applications
  call through, printing to a PDF (see [Printing](#printing)). `PrGeneral`
  answers that it can't do what's asked of it, as a printer without those
  features does.
- **Sound:** the Sound Driver's square-wave, four-tone and free-form
  sounds, and samples a program puts in the Mac Plus sound buffer, played
  through your computer's sound output (see [Sound](#sound)). The Sound
  Manager's original calls, `SndPlay` and the channels and their commands,
  play through the same buffer; `SndSoundManagerVersion` says it's
  version 2.0, System 7's. The later calls, from double buffering to
  recording and speech, aren't there yet.

Not yet:

- Color QuickDraw and offscreen GWorlds;
- Apple events and PPC sessions between applications;
- an application's own menu definitions (`MDEF`s), which are drawn as
  standard menus;
- the serial ports and AppleTalk;
- in custom Open and Save dialogs, the application's own text fields and
  user items, its event filter, and System 7's activation procedures.

An application that calls these gets on as far as it can without them (see
below).


## When it stops

### A call macrt doesn't implement

```
macrt: MyApp: NewGWorld (trap $AB1D) isn't implemented; skipped
```

The routine is known but has nothing behind it yet: here `NewGWorld`,
Color QuickDraw's, which comes through the trap `QDExtensions` ($AB1D).
Package routines are named the same way, by the routine their selector
picks rather than the package trap they come through. macrt takes its
arguments off the stack and returns 0, or `unimpErr` (-4) for a routine
that returns an error code. Many applications carry on regardless, so each
missing call is reported only once. When the application quits, macrt
counts them up:

```
macrt: MyApp called traps macrt doesn't implement: NewGWorld (1), PixMap32Bit (3)
```

That list is the most useful thing to report: it says exactly what an
application is waiting for.

### A system error

When the application does something it can't recover from, macrt stops it
and shows the classic system error alert. The other applications keep
running. The terminal gets the details:

```
macrt: MyApp stopped: system error 3 (illegal instruction): at $00002A
  PC 0000002A  SR 2000
  D0-D7 00000000 0000000C 00000000 00000000 00000000 00000000 00000000 00000000
  A0-A7 00000000 00084A10 00000000 00000000 00000000 001840F8 0017FE40 0017FE2C
  last traps: GetNextEvent at $08F2A4, …, Get1Resource at $0899C2, HLock at $0899CA
```

The first line says what went wrong. Here, MyApp jumped to $00002A, in
low memory, which isn't code (see the table below). Below it are the
registers and the last 16 traps, newest last, with the address each was
called from.

When the first line only names an exception, the last traps usually show
the cause. Often a call shortly before was skipped, and the application
went on with a value that call should have filled in.

| ID | Name | What it usually means here |
| --- | --- | --- |
| 0 | macrt itself failed | A bug in macrt, not in the application. A Python traceback comes with it. |
| 1 | bus error | The application read or wrote memory outside the Mac's 8 MB, or passed a bad pointer to a trap. Usually it's a handle from a call that failed earlier. |
| 2 | address error | It jumped to an odd address. |
| 3 | illegal instruction | It ran into data: often a call through a NIL procedure pointer, or a segment that isn't code. |
| 4 | zero divide | It divided by zero, often by a value a skipped call should have set. |
| 5 | check trap | A `CHK` instruction failed (Pascal range checking). |
| 6 | overflow trap | A `TRAPV` instruction failed. |
| 7 | privilege violation | It ran an instruction that isn't allowed. |
| 8 | trace trap | The trace bit was set. |
| 10 | line 1111 trap | An F-line instruction that the 68020 and its 68881 don't have, such as an MMU instruction. Floating-point instructions themselves work, whichever `--model` it is. |
| 11 | miscellaneous exception | Any other processor exception. |
| 12 | unimplemented trap | A trap macrt doesn't even know the name of, or a private call. PrintMonitor's private MultiFinder calls stop it here. |
| 15 | segment loader error | A `CODE` segment that's missing, compressed, or built for the far code model. |
| 25 | out of memory | A Toolbox call couldn't get the memory it had to have. Most calls report `memFullErr` instead and let the application decide. |

### Looking closer

- `--trace-traps` logs every trap as it's called, with the address it was
  called from. Use it to see what an application was doing before it
  stopped, or why it's sitting still. A package call is logged with the
  routine it's for, by Apple's name:

  ```
  macrt: Fly Swatter: FP68K FMULS at $1033F6
  macrt: Microsoft Excel: IUMagString at $1193A6
  ```
- `--view` shows the application's menus, windows and dialogs from its
  resources without running any code. If you mainly want to see what an
  application looked like, this always works.
- `--list` shows what the application is made of. An `ALRT` or `STR#`
  near the error often says what the application was trying to tell you.


## Writing a test application

`tests/apps` holds small 68k applications written in assembly, one per
part of the Toolbox: Hello (windows and menus), Controls, Dialogs, Edit
(TextEdit and the scrap), Lists (the List Manager), Menus (its own
`MBDF`), Frames (its own `WDEF`) and Launcher (`_Launch`). To build them you need
binutils for m68k (`apt install binutils-m68k-linux-gnu`). Then run:

```sh
python tests/apps/build.py
```

That assembles each `.s` file and wraps the code, with the resources it
needs, into a MacBinary file next to it. The built `.bin` files are checked
in, so the tests don't need the assembler. `tests/apps/traps.inc` defines
the trap words. Each application is one code segment, and the end of
`build.py` lists the resources each one gets. To try one, run it like any
other application:

```sh
python -m macrt tests/apps/Dialogs.bin --trace-traps
```
