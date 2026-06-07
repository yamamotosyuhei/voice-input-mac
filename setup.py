from setuptools import setup

APP = ['voice_input.py']
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'CFBundleName': 'VoiceInput',
        'CFBundleDisplayName': 'VoiceInput',
        # Customize this identifier if you have your own domain.
        # Same identifier across rebuilds = TCC permissions (mic / input monitoring / accessibility) are preserved.
        'CFBundleIdentifier': 'app.voiceinput',
        'CFBundleVersion': '1.0',
        'CFBundleShortVersionString': '1.0',
        'LSUIElement': True,  # Menubar-only (no Dock icon)
        'NSMicrophoneUsageDescription': 'VoiceInput uses the microphone for speech-to-text.',
        'NSAppleEventsUsageDescription': 'VoiceInput sends notifications.',
    },
    'packages': ['rumps'],
    'includes': ['Quartz', 'objc'],
}

setup(
    app=APP,
    name='VoiceInput',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
