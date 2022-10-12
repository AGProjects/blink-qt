Blink Translations (i18n)
=========================

Translate
---------

Blink translations should be made using the 'Qt Linguist' program.

You need to open `resources/i18n/blink__LANGUAGE_CODE__.ts` and translate the text.

After you're done, save the file and run `release_translations`.


Update translation files
------------------------

To update the translation files with the newly added text if the application changed you need to run:
`generate_translations`


Adding a language
-----------------

If you want to add a language to Blink, you need to change `blink-qt.pro` and add the language in the `TRANLATIONS` line.

The format needs to be `resources/i18n/blink_LANGUAGECODE_.ts`.

After this, edit `blink/preferences.py` and add the name of the language to the mapping:

```python
    mapping = {"default": "System default",
               "en": "English",
               "nl": "Nederlands",
               "ro": "Română"}
```

After this you can run `generate_translations` and proceed to translate the newly generated file.

