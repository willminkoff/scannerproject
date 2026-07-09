# SDRTrunk JVM shutdown-hook fork (clean SDRplay release)

A one-commit local fork of **SDRTrunk v0.6.1** that releases SDRplay devices on
`SIGTERM`/`SIGINT` (including headless operation), instead of only on GUI window-close.
Addresses macOS-transition-memo §C#3 (the "dirty RSPduo release"). Defensive correctness
improvement — see the caveat at the bottom.

## The change
`io.github.dsheirer.gui.SDRTrunk` only released tuners from `processShutdown()`, which runs
**only when the Swing window is closed**. A killed or headless process therefore never called
`mTunerManager.stop()` (→ `releaseDiscoveredTuners()` + `sdrplay_api_Close()`), leaving the
RSPduo open in the apiService until it timed out the dead client.

Fix (patch: `macos/sdrtrunk/patches/0001-jvm-shutdown-hook-release-sdrplay.patch`):
- New guarded `releaseTuners()` — `AtomicBoolean mTunersReleased`, runs `mTunerManager.stop()`
  at most once, touches **no GUI state** so it is headless-safe.
- A JVM shutdown hook (`Runtime.getRuntime().addShutdownHook`, thread `sdrtrunk-tuner-release-hook`)
  registered in the constructor right after `mTunerManager.start()`.
- `processShutdown()` now routes its tuner release through `releaseTuners()` (so GUI close and the
  hook can't double-release).

## Reproducing the build
SDRTrunk v0.6.1 requires **JDK 23 _with bundled JavaFX_** (build.gradle: "OpenJDK 22+ that
includes the JavaFX modules, e.g. Bellsoft Liberica"). Plain Temurin 23 fails with ~100
`cannot find symbol StringProperty/ObservableList` errors.

```bash
# 1. source (matches the installed jar exactly)
git clone --depth 1 --branch v0.6.1 https://github.com/DSheirer/sdrtrunk.git ~/SDRTrunk-src
cd ~/SDRTrunk-src
git apply macos/sdrtrunk/patches/0001-jvm-shutdown-hook-release-sdrplay.patch   # if starting clean

# 2. JDK 23 FULL (has JavaFX). Liberica, macOS x64:
#    https://download.bell-sw.com/java/23.0.2+9/bellsoft-jdk23.0.2+9-macos-amd64-full.tar.gz
export JAVA_HOME=~/jdks/jdk-23.0.2-full.jdk

# 3. build ONLY the app jar (the install is a jpackage layout; no runtime rebuild needed)
./gradlew --no-daemon -Porg.gradle.java.installations.paths="$JAVA_HOME" jar
# -> build/libs/sdr-trunk-0.6.1.jar
```

## Deploy / rollback
```bash
# deploy (stock is preserved on first deploy)
cp ~/SDRTrunk/lib/sdr-trunk-0.6.1.jar ~/SDRTrunk/lib/sdr-trunk-0.6.1.jar.orig-no-shutdownhook  # once
cp ~/SDRTrunk-src/build/libs/sdr-trunk-0.6.1.jar ~/SDRTrunk/lib/sdr-trunk-0.6.1.jar
# rollback
cp ~/SDRTrunk/lib/sdr-trunk-0.6.1.jar.orig-no-shutdownhook ~/SDRTrunk/lib/sdr-trunk-0.6.1.jar
```
Verify the hook is present: `javap -p -classpath ~/SDRTrunk/lib/sdr-trunk-0.6.1.jar \
io.github.dsheirer.gui.SDRTrunk | grep releaseTuners`.

## Caveat — measured, not theoretical
Live-tested 2026-06-27 on this box (macOS 15.7.7, SDRplay API 3.15.1): the ~22 s dirty-release
**did not reproduce**, even with the *stock* jar under SIGTERM, SIGTERM-while-streaming, or
SIGKILL — the apiService reclaims a dead client's device faster than SDRTrunk's ~8 s
boot-to-enumerate. So this hook is correct, upstream-quality hygiene but its practical benefit on
this host is currently **unproven**. The effective fix for the "[0] devices on restart" symptom is
single-consumer enforcement (`macos/bin/sdrctl`); this hook is belt-and-suspenders. Good candidate
to upstream as a PR rather than carry as a private fork.
