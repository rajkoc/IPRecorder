# IP Recorder

**Srpski** | [English](README.en.md)

Jednostavan program sa grafičkim prozorom za snimanje IP video strimova (RTSP, HTTP, UDP/multicast…) u **WMV** ili **H.264 MP4**. Snimanje obavlja pozadinski servis bez prozora, tako da se nastavlja i kad se prozor zatvori. Radi preko [ffmpeg](https://ffmpeg.org/).

## Mogućnosti

- Proizvoljan broj strimova (do 32): dodavanje i brisanje redova u prozoru
- Tri formata: **WMV**, **H.264 MP4** (rekodiranje) i **H.264 MP4 bez rekodiranja** (skoro bez opterećenja procesora)
- Podešavanje video i audio bitrate-a, rezolucije, FPS-a, šablona imena i foldera za snimke
- Deljenje fajlova na pun sat kao kod DVR-a (npr. 2 h: 00–02, 02–04…)
- Raspored snimanja: početak, kraj i dani u nedelji
- Pozadinski servis koji se može pokrenuti i zaustaviti iz prozora, uz opcioni automatski start pri prijavi na Windows
- Monitor uživo za jedan izabrani kanal, sa indikatorom zvuka (L/R)
- Automatsko ponovno povezivanje kad veza pukne
- Automatsko brisanje snimaka starijih od zadatog broja dana
- Ograničenje procesora (Windows 8+), prioritet procesa i broj niti

## Zahtevi

- Python 3.8 ili noviji (tkinter dolazi uz standardnu instalaciju), ili gotov `IPRecorder.exe`
- `ffmpeg` (preporučena verzija 5 ili novija). Za H.264 MP4 sa rekodiranjem mora imati `libx264`
- Windows (podržan je i Windows 7 uz Python 3.8); većina koda radi i na Linuxu

## Pokretanje

**Iz izvornog koda:**

```
python ip_recorder.py
```

**Gotov exe:** stavite `IPRecorder.exe` i `ffmpeg.exe` u isti folder i pokrenite `IPRecorder.exe`.

Pozadinski servis se pokreće iz prozora (dugme *Pokreni servis* ili prvim *Start*-om). Ručno: `python ip_recorder.py --service`.

## Brzi početak

1. U polju *ffmpeg putanja* izaberite `ffmpeg.exe` (ili ga stavite pored programa).
2. Izaberite *Folder za snimke*.
3. U listi strimova upišite naziv i URL kamere, npr. `rtsp://korisnik:lozinka@192.168.1.10:554/stream1`.
4. Izaberite *Format snimka* i kliknite *Start* (ili *Pokreni sve*).

Svako polje je objašnjeno u korisničkom uputstvu: [`ip_recorder_uputstvo.html`](ip_recorder_uputstvo.html).

## Pravljenje exe fajla

Lokalno (potrebni su Python 3.8 i internet):

```
pip install pyinstaller==5.13.2
pyinstaller --clean --onefile --noconsole --name IPRecorder ip_recorder.py
```

Rezultat je `dist/IPRecorder.exe`. Ili preko GitHub Actions: workflow `.github/workflows/build-exe.yml` pravi exe na Windows računaru u oblaku (*Actions → Build exe → Run workflow*), a gotov fajl se preuzima iz sekcije *Artifacts*.

## Fajlovi koje program pravi

Pored programa:

| Fajl | Sadržaj |
|------|---------|
| `ip_recorder_settings.json` | sva podešavanja |
| `ip_recorder_service.log` | dnevnik servisa (početak i prekid snimanja, greške) |
| `ip_recorder_error.log` | greške prozora |

## Napomene

- **Windows 7:** koristite Python 3.8 (poslednja verzija koja ga podržava). Tvrdi limit procesora zahteva Windows 8 ili noviji.
- Multicast adrese u opsegu `232.x.x.x` (SSM) traže adresu izvora: `udp://232.0.12.9:5000?sources=IP_IZVORA`.
- Servis sluša samo na `127.0.0.1:47653` i ne izlaže se mreži.
- Brisanje starih snimaka je trajno i dotiče samo `.wmv` i `.mp4` fajlove čije ime počinje nazivom neke kamere.
- Posle zamene `ip_recorder.py` novom verzijom restartujte servis (prozor sam ponudi restart).

## Licenca

Nije određena. Dodajte `LICENSE` fajl po želji.
