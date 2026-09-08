# Client Config — Firebase

These artifacts are environment-specific and contain project-bound
identifiers. They must be generated against the real Firebase project
(`mbp-tracker-dev` for development) by a developer with console access;
they are **not** checked into source control.

## Generated paths (placeholders only — empty until `flutterfire configure` runs)

| Platform | Path                                           | Source                                                 |
| -------- | ---------------------------------------------- | ------------------------------------------------------ |
| iOS      | `flutter/ios/Runner/GoogleService-Info.plist`  | Firebase Console → Project Settings → iOS app          |
| Android  | `flutter/android/app/google-services.json`     | Firebase Console → Project Settings → Android app      |
| Web      | embedded in `flutter/lib/firebase_options.dart` (the `web` entry of `DefaultFirebaseOptions`) | `flutterfire configure` |
| Dart     | `flutter/lib/firebase_options.dart`            | `flutterfire configure`                                |

## Steps

```bash
# Once, install the CLI helpers.
dart pub global activate flutterfire_cli
npm i -g firebase-tools

# Log in.
firebase login

# From the Flutter project root.
cd flutter
flutterfire configure \
  --project=mbp-tracker-dev \
  --platforms=ios,android,web \
  --ios-bundle-id=com.example.mbptracker \
  --android-package-name=com.example.mbptracker
```

The command writes `firebase_options.dart` and drops the iOS plist /
Android JSON into the expected locations. Add the generated files to
`.gitignore` (or to a secrets management workflow) — do not commit them.

After configuration, initialize Firebase in `main.dart`:

```dart
await Firebase.initializeApp(
  options: DefaultFirebaseOptions.currentPlatform,
);
```

## Verification

- iOS: `flutter build ios --no-codesign` succeeds.
- Android: `flutter build apk --debug` succeeds.
- Web: `flutter run -d chrome` boots without "Firebase not initialized".
