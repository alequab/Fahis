<div dir="rtl" align="center">

**بسم الله الرحمن الرحيم**

**والصلاة والسلام على نبينا محمد ﷺ**

</div>

<img src="electron/icon.svg" alt="أيقونة فاحص: عدسة مكبرة بداخلها حرف الفاء" width="96">

# فاحص (Fahis)

**[العربية](#العربية) | [English](#english)**

---

<div dir="rtl" lang="ar">

## العربية

اعرف ما تُبلغ عنه سيارتك من خلال حاسوب محمول يعمل بنظام Windows ومحوّل ELM327 رخيص يعمل بتقنية Bluetooth LE.

كثير من محوّلات ELM327 تعمل بتقنية Bluetooth LE ‏(BLE)، ولا يعطيها Windows منفذ COM، لذلك لا تجدها معظم برامج OBD-II. أما فاحص فيتصل بالمحوّل مباشرة عبر BLE.

![تطبيق فاحص باللغة العربية، ويعرض بيانات محاكاة](docs/app-overview-ar.png)

*لقطة من الوضع التجريبي، فالبيانات المعروضة محاكاة.*

### المزايا

- **تطبيق سطح مكتب:** نافذة Electron فيها صفحات للنظرة العامة والبيانات المباشرة ورموز الأعطال والأوامر والسيارات المدعومة والسيارات المحفوظة والتقارير
- **تشغيل الأوامر من التطبيق:** ابدأ فحص البطارية والتقرير والبحث عن الوحدات وغيرها، واقرأ النتائج في الصفحة
- **العربية والإنجليزية:** بدّل اللغة بزر واحد، فيتحول التطبيق أيضًا من اليمين إلى اليسار، أو استخدم `--lang` في الطرفية
- **رموز الأعطال:** المخزّنة والمعلّقة والدائمة، مع أوصاف لنحو 9,500 رمز عام
- **وحدات أخرى:** يبحث عن وحدات غير المحرك ويقرأ رموز أعطالها إذا سمحت السيارة بذلك
- **سيارات كثيرة:** قائمة قابلة للبحث تضم نحو 880 ملف سيارة من المجتمع، تُنزَّل عند الحاجة
- **حفظ السيارات:** يحفظ كل سيارة تتصل به مع بياناتها وآخر قراءاتها، على جهازك فقط
- **فحص البطارية:** يشرح الجهد بلغة بسيطة، ويعتمد على قراءة السيارة نفسها إذا توفرت
- **تقرير كامل وسجل CSV:** احفظ قراءة كاملة في ملف نصي، أو سجّل القيم المباشرة لفتحها في جدول بيانات
- **مسح محميّ:** لا يعمل مسح الرموز إلا والمحرك متوقف، وبعد أن تكتب `CLEAR`

### المتطلبات

- نظام Windows 10 أو 11 مع Bluetooth
- ‏Python 3.12 (الإصدارات الأخرى لم تُختبر)
- ‏Node.js لتشغيل تطبيق سطح المكتب
- محوّل ELM327 يعمل بتقنية BLE ويوفر الخدمة التسلسلية `FFF0`
- سيارة تستخدم CAN بعناوين 11 بت، وهذا حال معظم السيارات منذ نحو عام 2008

المحوّلات التي تعمل بتقنية Bluetooth التقليدية أو USB أو Wi-Fi غير مدعومة.

### التثبيت

<div dir="ltr">

```
git clone https://github.com/alequab/fahis.git
cd fahis
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
npm install
npm run setup
```

</div>

ثم:

1. وصّل المحوّل بمنفذ OBD-II في السيارة.
2. شغّل المحرك في مكان مفتوح، أو أدر مفتاح الإشعال.
3. أقرن المحوّل من إعدادات Bluetooth في Windows. إذا طلب رمز PIN فجرّب `1234` أو `0000` أو `6789`.
4. أوقف Bluetooth في أي هاتف اتصل بالمحوّل من قبل.

### الاستخدام

شغّل تطبيق سطح المكتب من مجلد المشروع:

<div dir="ltr">

```
npm start
```

</div>

يواصل التطبيق البحث عن المحوّل حتى تستجيب السيارة. لتجربته ببيانات محاكاة دون سيارة، شغّل `npm run demo`. وعند إغلاق النافذة تتوقف الخدمة الخلفية وينقطع الاتصال بالمحوّل.

ويمكنك أيضًا تشغيل الأوامر في الطرفية بدلًا من التطبيق:

| الأمر | ماذا يفعل |
|---|---|
| `.\obd report` | يقرأ كل البيانات مرة واحدة ويحفظها في مجلد `reports\` |
| `.\obd info` | المحوّل والجهد ولمبة فحص المحرك والاختبارات الذاتية |
| `.\obd battery` | جهد البطارية مع شرح بلغة بسيطة |
| `.\obd codes` | رموز الأعطال المخزّنة والمعلّقة والدائمة |
| `.\obd scan` | يبحث عن وحدات أخرى ويقرأ رموز أعطالها إذا سُمح بذلك |
| `.\obd cars` | يعرض السيارات المحفوظة على هذا الجهاز، دون محوّل |
| `.\obd profiles toyota` | يبحث في قائمة ملفات السيارات، دون محوّل |
| `.\obd profile --name kia-picanto --year 2017` | يقرأ قيمًا إضافية لسيارتك، وينزّل ملفها عند الحاجة |
| `.\obd live` | القيم المباشرة في الطرفية |
| `.\obd log` | القيم المباشرة مع حفظها بصيغة CSV في مجلد `logs\` |
| `.\obd clear` | يمسح رموز الأعطال بعد أن يطلب منك التأكيد |

اضغط Ctrl+C لإيقاف `live` أو `log`. يخدم المحوّل برنامجًا واحدًا في كل مرة، لذلك أغلق التطبيق قبل تشغيل أمر في الطرفية.

**الخيارات**

- ‏`--lang ar` أو `--lang en` يحدد اللغة ويحفظها، ويُكتب قبل الأمر، مثل `.\obd --lang ar report`.
- ‏`--address AA:BB:CC:DD:EE:FF` يختار محوّلًا محددًا، ويُكتب قبل الأمر. يبحث فاحص افتراضيًا عن محوّل يحتوي اسمه على `OBD` أو `ELM`.
- ‏`--interval 1` يحدد عدد الثواني بين القراءات، للأمرين `live` و`log`.
- ‏`--samples 20` يوقف البرنامج بعد 20 قراءة، للأمرين نفسيهما.
- ‏`.\obd profiles --get toyota-camry` ينزّل ملف السيارة الآن لتستخدمه لاحقًا دون إنترنت.

### السلامة

- **لا تستخدم التطبيق أثناء القيادة.** استخدم `.\obd log`، أو اجعل أحد الركاب يتابعه.
- **انتبه إلى البطارية.** تُستنزف البطارية عندما يكون الإشعال يعمل والمحرك متوقفًا. افصل المحوّل عند الانتهاء، لأن بعض المحوّلات تواصل سحب الكهرباء.
- **مسح الرموز لا يصلح العطل.** وهو يمحو أيضًا بيانات الأعطال المحفوظة ويعيد ضبط اختبارات الانبعاثات الذاتية.
- **كل الأوامر ما عدا `clear` تقرأ البيانات فقط.** لا يرسل `scan` و`profile` إلا طلبات قراءة، ويرفض البرنامج ما سواها.
- **لا يبدأ `scan` و`profile` إذا كان الجهد أقل من 11.8 فولت،** لأن القراءات الطويلة تستنزف البطارية.

### الخصوصية

قد تحتوي التقارير والسيارات المحفوظة على رقم الهيكل (VIN)، وتسجّل ملفات السجل طريقة استخدام السيارة. كل ذلك يبقى على جهازك، ويمنع ملف `.gitignore` رفعه إلى المستودع. احذف ملف السيارة من مجلد `cars\` إذا أردت نسيانها. تتصل النافذة بخدمتها الخلفية عبر قناة خاصة، دون خادم ويب أو منفذ شبكة مفتوح، ولا تستطيع الصفحة تحميل أي شيء من الإنترنت. وتنزيل ملف سيارة هو المرة الوحيدة التي يتصل فيها التطبيق نفسه بالإنترنت.

### الحدود

- اختُبر البرنامج مع محوّل BLE واحد يعرّف نفسه بأنه `ELM327 v2.1` على سيارتين من Kia، إحداهما بنزين والأخرى ديزل، وكلتاهما تستخدم CAN بسرعة 500 kbps. تعرض صفحة السيارات المدعومة السيارات التي تتوفر لها بيانات من المجتمع، ومعظمها لم يُختبر.
- لم تُختبر الأوامر التي تُشغَّل من صفحة الأوامر في التطبيق حتى الآن إلا في الوضع التجريبي. وقد اختُبرت الأوامر نفسها على سيارات حقيقية من الطرفية، ما عدا `clear` الذي لم يُشغَّل على سيارة من أي منهما.
- يعمل التطبيق من الشيفرة المصدرية دون برنامج تثبيت حتى الآن، ويحتاج إلى Python وNode.js.
- تغطي معظم الأوامر بيانات المحرك والانبعاثات القياسية. في السيارتين اللتين اختُبر عليهما، لم تستجب للأمر `scan` إلا وحدتا المحرك وناقل الحركة، فلم تُقرأ أعطال ABS والوسائد الهوائية والهيكل. وتختلف النتائج من سيارة إلى أخرى.
- قيم ملفات السيارات مصدرها بيانات المجتمع، وقد لا تطابق سنة سيارتك بالضبط، وقد لا ينطبق بعضها على سيارتك أصلًا.
- يتخطى `profile` الأوامر التي لا يستطيع إرسالها بأمان أو لا يدعمها: وهي التي تفتح جلسة تشخيص خاصة، والتي تستخدم عناوين 29 بت أو العناوين الموسّعة. وقد تفقد بعض السيارات كثيرًا من البيانات بسبب ذلك.
- تغطي أوصاف رموز الأعطال نحو 9,500 رمز عام. أما الرموز الخاصة بالشركات المصنّعة فيظهر تصنيفها فقط وتحتاج إلى بحث.
- في العربية تُرجم نحو 80 رمز عطل شائعًا، وتبقى الرموز الأخرى وأسماء قيم ملفات السيارات بالإنجليزية.
- قد تظهر العربية بشكل سيئ في طرفية Windows، لأن بعض الطرفيات لا تصل الحروف ولا تدعم الكتابة من اليمين إلى اليسار. التطبيق وملفات التقارير أفضل لقراءة العربية.
- قد تكون قراءة الجهد من المحوّل غير دقيقة: ففي الإعداد الذي جرى اختباره كانت أعلى بنحو 1 فولت من قراءة وحدة السيارة.
- تُحدَّث القيم المباشرة واحدة تلو الأخرى، لذلك قد يستغرق تحديث القيم الأبطأ عدة ثوانٍ.

### التطوير

شغّل الاختبارات التي لا تحتاج إلى سيارة أو محوّل:

<div dir="ltr">

```
.venv\Scripts\python -m unittest test_obd_tool
```

</div>

| الملف | وظيفته |
|---|---|
| `obd_tool.py` | الاتصال بالمحوّل وتفسير بيانات OBD-II والأوامر |
| `car_modules.py` | البحث عن الوحدات وتفسير ملفات السيارات |
| `garage.py` | سجلات السيارات المحفوظة |
| `i18n.py` | النصوص العربية والإنجليزية |
| `electron/main.js` و`electron/preload.js` | نافذة سطح المكتب، وهي تشغّل الخدمة الخلفية وتوقفها |
| `electron/index.html` | صفحات التطبيق |
| `electron/icon.svg` و`icon-small.svg` و`icon.ico` و`icon.png` | أيقونة التطبيق: الرسم الأصلي، ونسخة مبسطة لمقاسي 16 و24 بكسل، والملفات الجاهزة |
| `bridge.py` | القناة الخاصة بين النافذة والخدمة الخلفية |
| `demo.py` | محوّل محاكاة للوضع التجريبي |
| `dtc_codes.py` | البحث عن أوصاف رموز الأعطال |
| `profiles\` | قائمة السيارات وملف Kia Picanto، من OBDb |
| `data\` | أوصاف رموز الأعطال العامة، من OBDex |
| `test_obd_tool.py` | الاختبارات |

### الترخيص

يمكن **للأفراد** استخدام فاحص وتعديله ومشاركته مجانًا للأغراض الشخصية. **ولا يحق للشركات والمؤسسات استخدامه** بدون إذن كتابي، ويشمل ذلك العاملين لحسابهم الخاص والأعمال الفردية عند استخدامه في عملهم. ولا يجوز بيعه. راجع ملف [LICENSE](LICENSE) للاطلاع على الشروط كاملة، والنص الإنجليزي هو المعتمد.

بسبب هذه القيود، يُعد فاحص مشروعًا متاح الشيفرة للاطلاع (source-available) وليس مفتوح المصدر بالمعنى المعروف. وتبقى المكونات الخارجية المضمنة فيه خاضعة لرخصها الخاصة، وهي مذكورة في [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

</div>

---

## English

See what your car is reporting, on a Windows laptop, using a cheap Bluetooth LE ELM327 adapter.

Many ELM327 adapters are Bluetooth LE (BLE). Windows gives them no COM port, so most OBD-II programs can't find them. Fahis talks to the adapter directly over BLE.

![The Fahis desktop app in English, showing simulated data](docs/app-overview-en.png)

*Screenshot from demo mode, so the data is simulated.*

### Features

- **Desktop app:** an Electron window with pages for the overview, live data, fault codes, commands, supported cars, saved cars and reports
- **Run commands in the app:** start a battery check, report, module scan and more, and read the results on the page
- **Arabic and English:** switch with one button, which also flips the app to right-to-left, or use `--lang` in the terminal
- **Fault codes:** stored, pending and permanent, with descriptions for about 9,500 generic codes
- **Other modules:** looks for modules beyond the engine and reads their fault codes where the car allows it
- **Many cars:** searchable catalog of about 880 community car profiles, downloaded when you need one
- **Saved cars:** remembers each car that connects, with its details and latest readings, on your laptop only
- **Battery check:** explains the voltage in plain language, using the car's own reading when available
- **Full report and CSV logging:** save a full reading to a text file, or record live values for a spreadsheet
- **Protected clearing:** clearing codes only works with the engine off, and you must type `CLEAR` first

### What you need

- Windows 10 or 11 with Bluetooth
- Python 3.12 (other versions are untested)
- Node.js, for the desktop app
- A BLE ELM327 adapter that offers the `FFF0` serial service
- A car that uses 11-bit CAN, which is most cars from about 2008 onward

Classic Bluetooth, USB and Wi-Fi adapters aren't supported.

### Setup

```
git clone https://github.com/alequab/fahis.git
cd fahis
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
npm install
npm run setup
```

Then:

1. Plug the adapter into the car's OBD-II port.
2. Start the engine outdoors, or turn the ignition ON.
3. Pair the adapter in Windows Bluetooth settings. If it asks for a PIN, try `1234`, `0000` or `6789`.
4. Turn off Bluetooth on any phone that has connected to the adapter.

### Usage

Start the desktop app from the project folder:

```
npm start
```

It keeps looking for the adapter until the car answers. To try it with simulated data and no car, run `npm run demo`. Closing the window also stops the backend and disconnects from the adapter.

You can also run commands in a terminal instead of the app:

| Command | What it does |
|---|---|
| `.\obd report` | Reads everything once and saves it in `reports\` |
| `.\obd info` | Adapter, voltage, check engine light and self-tests |
| `.\obd battery` | Battery voltage with a plain-language explanation |
| `.\obd codes` | Stored, pending and permanent fault codes |
| `.\obd scan` | Looks for other modules and reads their fault codes where allowed |
| `.\obd cars` | Lists cars saved on this laptop, no adapter needed |
| `.\obd profiles toyota` | Searches the car profile catalog, no adapter needed |
| `.\obd profile --name kia-picanto --year 2017` | Reads extra values for your car, downloading its profile if needed |
| `.\obd live` | Live values in the terminal |
| `.\obd log` | Live values, saved as CSV in `logs\` |
| `.\obd clear` | Clears fault codes, after asking you to confirm |

Press Ctrl+C to stop `live` or `log`. The adapter serves one program at a time, so close the app before running a command in a terminal.

**Options**

- `--lang ar` or `--lang en` sets the language and remembers it. It goes before the command, such as `.\obd --lang ar report`.
- `--address AA:BB:CC:DD:EE:FF` picks a specific adapter. It goes before the command. By default, Fahis looks for one with `OBD` or `ELM` in its name.
- `--interval 1` sets the seconds between readings, for `live` and `log`.
- `--samples 20` stops after 20 readings, for the same commands.
- `.\obd profiles --get toyota-camry` downloads a profile now, so you can use it later without internet.

### Safety

- **Don't use the app while driving.** Use `.\obd log`, or have a passenger watch it.
- **Watch your battery.** With the ignition on and the engine off, the battery drains. Unplug the adapter when you finish, because some adapters keep drawing power.
- **Clearing codes doesn't fix a fault.** It also erases saved fault data and resets the emissions self-tests.
- **Every command except `clear` only reads data.** `scan` and `profile` can only send read requests, and the code refuses anything else.
- **`scan` and `profile` refuse to start below 11.8 V,** because long reads drain the battery.

### Privacy

Reports and saved cars can contain your VIN, and logs record how the car was used. All of them stay on your laptop, and `.gitignore` keeps them out of the repo. Delete a file in `cars\` to forget that car. The window talks to its backend through a private pipe, with no web server or open network port, and the page can't load anything from the internet. Downloading a car profile is the only time the app itself goes online.

### Limits

- It has been tested with one BLE adapter reporting `ELM327 v2.1` on two Kia cars, one gasoline and one diesel, both using 500 kbps CAN. The Supported cars page lists cars with community data, and most of them are untested.
- Commands run from the app's Commands page have only been tested in demo mode so far. The same commands have been tested on real cars from the terminal, except `clear`, which hasn't been run on a car from either place.
- The app is run from source, with no installer yet, and needs Python and Node.js.
- Most commands cover standard engine and emissions data. On both tested cars, only the engine and transmission modules answered `scan`, so ABS, airbag and body faults weren't readable. Results depend on the car.
- Profile values come from community data and may not match your car's exact year, and some may not apply to your car at all.
- `profile` skips commands it can't send safely or doesn't support: those that open a special diagnostic session, and those using 29-bit or extended addressing. Some cars lose a lot of data this way.
- Fault code descriptions cover about 9,500 generic codes. Manufacturer-specific codes show a category and need looking up.
- In Arabic, about 80 common fault codes are translated. Others, and profile value names, stay in English.
- Arabic in the Windows terminal may display poorly, because some terminals don't join letters or support right-to-left text. The app and saved report files are better for reading Arabic.
- The adapter's own voltage reading can be off: on the tested setup it read about 1 V above the car's module.
- Live values update one at a time, so slower ones can take several seconds to refresh.

### Development

Run the offline tests, which don't need a car or adapter:

```
.venv\Scripts\python -m unittest test_obd_tool
```

| File | Purpose |
|---|---|
| `obd_tool.py` | Adapter connection, OBD-II decoding and commands |
| `car_modules.py` | Module scan and car profile decoding |
| `garage.py` | Saved car records |
| `i18n.py` | English and Arabic text |
| `electron/main.js`, `electron/preload.js` | Desktop window, which starts and stops the backend |
| `electron/index.html` | The app's pages |
| `electron/icon.svg`, `icon-small.svg`, `icon.ico`, `icon.png` | The app's icon: artwork, a simplified 16 and 24 pixel version, and rendered files |
| `bridge.py` | Private pipe between the window and the backend |
| `demo.py` | Simulated adapter for demo mode |
| `dtc_codes.py` | Fault code lookups |
| `profiles\` | Car catalog and the Kia Picanto profile, from OBDb |
| `data\` | Generic fault code descriptions, from OBDex |
| `test_obd_tool.py` | Offline tests |

### License

Fahis is free for **individuals** to use, modify and share for personal purposes. **Companies and organizations may not use it** without written permission, including freelancers and one-person businesses using it for their work. It may not be sold. See [LICENSE](LICENSE) for the full terms.

Because of these limits, Fahis is source-available rather than open source. The third-party components it includes keep their own licenses, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
