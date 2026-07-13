# UI redesign — a real desktop application

## What is wrong today
Material-web look: centered cards, big rounded buttons, floating layout, one
content column, wizard as full-page steps. Reference (İmzala Masaüstü) reads
as a desktop tool because of: flat compact menu row, an icon toolbar, a
master area + right properties panel, thin separators, small type, a status
bar. We adopt that language wholesale.

## Layout (single window, master–detail)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Taşıma   Görünüm   Yardım                                     (menu row) │
├──────────────────────────────────────────────────────────────────────────┤
│ [＋Yeni] [⧉Toplu] │ [⏸Duraklat] [▶Devam] [⟳Eşitle] [✕İptal] │ [⚙Ayarlar] │
├───────────────────────────────────────────────┬──────────────────────────┤
│ TAŞIMALAR                    durum  ilerleme  │  SEÇİLİ TAŞIMA           │
│ ──────────────────────────────────────────────│  a@x → b@y               │
│ ● a@x.com → b@y.com        Çalışıyor ▓▓▓░ 62% │  Durum: Çalışıyor        │
│ ● c@x.com → d@y.com        Sırada        —    │  Klasör: INBOX           │
│ ○ e@x.com → f@y.com        Tamamlandı  100%   │  3.412 / 5.520 mesaj     │
│ ○ g@x.com → h@y.com        Duraklatıldı  41%  │  ▓▓▓▓▓▓░░░░ %62          │
│                                               │  ────────────────────    │
│   (satır seç → sağ panel; çift tık → odak)    │  Kaynak  imap.x.com:993  │
│                                               │  Hedef   imap.y.com:993  │
│                                               │  [Bağlantıyı düzenle]    │
├───────────────────────────────────────────────┴──────────────────────────┤
│ 2 taşıma çalışıyor                                              v0.2.0   │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Sol: taşıma listesi** — kart değil, sıkı tablo satırları (durum noktası,
  çift, durum, ince progress). Seçim → sağ panel; işlemler toolbar'dan
  (seçime duyarlı: koşana Duraklat, duranana Devam...).
- **Sağ: özellik paneli** — seçili taşımanın canlı detayı + bağlantı
  düzenleme (bugünkü detail sayfası buraya taşınır; ayrı sayfa ölür).
- **Yeni taşıma / Toplu**: tam sayfa sihirbaz yerine **modal dialog**
  (kaynak+hedef alanları tek formda, "Bağlantıyı sına" inline, ikinci adım
  klasör planı aynı dialog içinde). Desktop kalıbı bu.
- **Ayarlar**: modal dialog (sayfa değil).
- **Durum çubuğu**: sol "Hazır / N taşıma çalışıyor", sağ sürüm.
- Yoğunluk: 12–13px metin, 4–8px dolgular, hairline (%12 opak) ayraçlar,
  elevation yok, kart yok. Koyu/açık tema otomatik.

## Navigation model
Tek görünüm. Sayfa yığını (page.views) kalkar; dialoglar + sağ panel her
şeyi taşır. Poll yalnız liste satırlarını ve sağ paneli yeniler (in-place,
mevcut refs mekanizması korunur).

## Packaging polish (bundled with this work)
- macOS bundle adı: `Email Export Import Tool.app` (CI'da rename; dmg/zip
  içinde de bu adla).

## Test strategy
- View builders unit-testable aynı kalıpta (build_run_list, build_side_panel,
  build_wizard_dialog...).
- e2e harness: satır seçimi, toolbar'ın seçime göre aktifleşmesi, dialog
  akışları (mevcut _click/_clickables altyapısı).

## Order of work
1. UI redesign (bu spec) — engine arayüzü değişmez.
2. Daemon (ayrı spec) — RunManager yerine DaemonClient; UI dokunuşu minimal.
3. İkisi de bitince tek release + tüm eski sürümler silinir.
