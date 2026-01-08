# SDRTrunk Profiles

Each subdirectory in this folder represents a Ground profile intended for SDRTrunk.

- `profile.json` holds human-readable metadata used by the UI.
- If you want the profile to be runnable in headless mode, copy your SDRTrunk
  configuration into `sdrtrunk/` inside the profile directory.

Recommended layout per profile:

```
profiles/sdrtrunk/<profile-id>/
  profile.json
  sdrtrunk/
    playlist/
      default.xml
    configuration/
      ...
```

The sync script copies `sdrtrunk/` into the runtime SDRTrunk home at startup or
when the UI activates a profile.

Tip: build the playlist in SDRTrunk GUI, then copy the `~/SDRTrunk` directory
contents into the profile's `sdrtrunk/` folder.
