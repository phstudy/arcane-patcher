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

import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.regex.Pattern;

/**
 * A server runtime of the offline build: an interpreter executable (with its libraries) for one CPU. The APK carries
 * one per entry of the patcher's kit.SERVER_ABIS, all in the game's ABI folder under their own names, listed in the
 * asset RUNTIMES (one line each: name, interpreter file name, the Android ABIs it runs on natively).
 *
 * The runtime is chosen from the device's real CPU (Build.SUPPORTED_ABIS, in the device's order of preference), not
 * from the app process: an x86_64 PC emulator runs the arm64 game through ARM translation, which does not cover the
 * server process the game starts. If the chosen runtime fails to start, the next one is tried once.
 *
 * Android-free (no Android classes): tests run it on a desktop JVM (main).
 */
final class ServerRuntime {
    static final String RUNTIMES = "arcane_offline/runtimes";
    /** A debug property naming the runtime to try first (tests: the fallback on a device the runtime cannot run on). */
    static final String FORCE_PROPERTY = "debug.arcane.server_runtime";
    /** How long a started runtime is watched: a wrong-CPU or unlinkable executable fails within milliseconds. */
    static final long PROBE_MS = 1500;

    final String name;
    final String executable;
    final List<String> deviceAbis;

    ServerRuntime(String name, String executable, List<String> deviceAbis) {
        this.name = name;
        this.executable = executable;
        this.deviceAbis = deviceAbis;
    }

    @Override
    public String toString() {
        return name;
    }

    /** The runtimes of the RUNTIMES asset, in its order (the first is the default). */
    static List<ServerRuntime> parse(String table) {
        List<ServerRuntime> out = new ArrayList<>();
        for (String line : table.split("\n")) {
            String[] f = line.trim().split("\\s+");
            if (f.length != 3) continue;
            out.add(new ServerRuntime(f[0], f[1], Arrays.asList(f[2].split(","))));
        }
        if (out.isEmpty()) throw new IllegalStateException("no server runtimes in " + RUNTIMES);
        return out;
    }

    /**
     * The runtimes in the order to try them: `forced` (a runtime's name) if given, else the one for the first of the
     * device's ABIs that one runs on (the first runtime if none does); then the others.
     */
    static List<ServerRuntime> order(List<ServerRuntime> all, String[] deviceAbis, String forced) {
        ServerRuntime first = null;
        for (ServerRuntime r : all) if (r.name.equals(forced)) first = r;
        for (int i = 0; first == null && i < deviceAbis.length; i++) {
            for (ServerRuntime r : all) {
                if (r.deviceAbis.contains(deviceAbis[i])) {
                    first = r;
                    break;
                }
            }
        }
        if (first == null) first = all.get(0);
        List<ServerRuntime> out = new ArrayList<>(Collections.singletonList(first));
        for (ServerRuntime r : all) if (r != first) out.add(r);
        return out;
    }

    /** What the shell or the dynamic linker says when the executable cannot run on this CPU or cannot be linked. */
    private static final Pattern START_ERROR = Pattern.compile(
            "syntax error|not found|cannot execute|can't execute|Exec format error|CANNOT LINK EXECUTABLE|cannot load");

    /** How one runtime's start went: its process, its exit code if it ended within PROBE_MS, and what it printed. */
    static final class Attempt {
        final Process process;
        final Integer exitCode;
        final String output;

        Attempt(Process process, Integer exitCode, String output) {
            this.process = process;
            this.exitCode = exitCode;
            this.output = output;
        }

        /** It did not start: it ended at once, or the shell / linker said it cannot run (lines naming it). */
        boolean failed(ServerRuntime r) {
            if (exitCode != null) return true;
            for (String line : output.split("\n")) {
                if ((line.contains(r.executable) || line.startsWith("CANNOT LINK") || line.startsWith("arcanepython:"))
                        && START_ERROR.matcher(line).find()) return true;
            }
            return false;
        }
    }

    interface Starter {
        /** Start `r` and watch it for PROBE_MS. */
        Attempt start(ServerRuntime r) throws IOException;
    }

    interface Log {
        void line(String text);
    }

    /** The runtime that runs, and its process. */
    static final class Started {
        final ServerRuntime runtime;
        final Process process;

        Started(ServerRuntime runtime, Process process) {
            this.runtime = runtime;
            this.process = process;
        }
    }

    /**
     * Start the runtimes in `order` until one starts: the first, then each next one once. If none starts, the first
     * stays the runtime (later restarts use it) and the last attempt's process is returned (it has ended).
     */
    static Started start(List<ServerRuntime> order, Starter starter, Log log) throws IOException {
        IOException lastError = null;
        Process last = null;
        for (int i = 0; i < order.size(); i++) {
            ServerRuntime r = order.get(i);
            String next = i + 1 < order.size() ? "; trying " + order.get(i + 1) : "";
            log.line("server runtime " + r + ": starting " + r.executable);
            Attempt a;
            try {
                a = starter.start(r);
            } catch (IOException e) {
                lastError = e;
                log.line("server runtime " + r + " failed to start: " + e + next);
                continue;
            }
            if (!a.failed(r)) {
                log.line("server runtime " + r + ": running");
                return new Started(r, a.process);
            }
            if (a.exitCode == null && a.process != null) a.process.destroy();
            last = a.process;
            lastError = null;
            log.line("server runtime " + r + " failed to start ("
                    + (a.exitCode != null ? "exit code " + a.exitCode : "still running") + "): "
                    + firstLine(a.output) + next);
        }
        log.line("no server runtime started; " + order.get(0) + " is retried");
        if (lastError != null) throw lastError;  // the last one could not even be started
        return new Started(order.get(0), last);
    }

    private static String firstLine(String s) {
        String t = s.trim();
        int nl = t.indexOf('\n');
        return nl < 0 ? t : t.substring(0, nl);
    }

    /**
     * Tests (desktop JVM): `order TABLE ABIS FORCED` prints the order; `start TABLE OUTCOME...` runs start() with one
     * outcome per attempt ("ok", "exit:CODE:OUTPUT", "alive:OUTPUT" or "throw:MESSAGE") and prints the log and the
     * runtime that runs. TABLE uses ';' between lines, ABIS ','; FORCED "-" for none.
     */
    public static void main(String[] args) throws IOException {
        List<ServerRuntime> all = parse(args[1].replace(';', '\n'));
        if (args[0].equals("order")) {
            String forced = args.length > 3 && !args[3].equals("-") ? args[3] : null;
            System.out.println(order(all, args[2].isEmpty() ? new String[0] : args[2].split(","), forced));
            return;
        }
        final List<String> outcomes = new ArrayList<>(Arrays.asList(args).subList(2, args.length));
        Started s = start(all, r -> {
            String o = outcomes.remove(0);
            if (o.equals("ok")) return new Attempt(null, null, "");
            if (o.startsWith("throw:")) throw new IOException(o.substring(6));
            if (o.startsWith("alive:")) return new Attempt(null, null, o.substring(6));
            String[] f = o.split(":", 3);
            return new Attempt(null, Integer.parseInt(f[1]), f.length > 2 ? f[2] : "");
        }, System.out::println);
        System.out.println("runtime " + s.runtime);
    }
}
