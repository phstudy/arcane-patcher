/*
 * SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
 * Copyright (C) the arcane-patcher contributors
 *
 * This file is part of arcane-patcher: free software under the GNU General Public License version 3 only, with the
 * additional permission (GPL section 7) in LICENSES/AdditionRef-arcane-client-linking.txt: this Program, or any
 * covered work, may be linked or combined with the game client it is applied to. That permission grants no rights to
 * the game client itself, which belongs to its owners; this project does not distribute it.
 */
package arcane.offline;

/**
 * The setup screen reached while the game runs: the app's settings entry in Android's app info
 * (APPLICATION_PREFERENCES) and the launcher's long-press shortcut. It has its own task (manifest taskAffinity), so
 * opening it never touches the game's task. While the game runs it changes the speed / account by restarting the app
 * (SetupActivity: settings mode); when the app is not running it is the ordinary setup screen.
 */
public final class SettingsActivity extends SetupActivity {}
