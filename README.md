# pm-deney — Adım 1: Günlük Polymarket veri toplama

30 günlük tahmin-kalibrasyon deneyinin veri katmanı. Her gün 09:00 (TR) GitHub Actions
Polymarket'ten hacme göre ilk 100 aktif piyasayı çeker ve repoya commit eder.

## Kurulum (5 dakika)

1. GitHub'da yeni repo aç (`pm-deney`), bu klasördeki dosyaları yükle.
2. Repo → **Settings → Actions → General → Workflow permissions** → "Read and write permissions" seç → Save.
3. Repo → **Actions** sekmesi → `gunluk-veri-cek` → **Run workflow** (ilk çalıştırma elle).
4. 1–2 dakika sonra `data/` altında dosyalar belirmeli. Belirmezse Actions log'unu at, beraber bakarız.

## Üretilen dosyalar

| Dosya | İçerik |
|---|---|
| `data/raw/YYYY-MM-DD.json` | API'nin ham cevabı (alan adı değişse de veri kaybolmaz) |
| `data/snapshots/YYYY-MM-DD.csv` | O günün ayrıştırılmış tablosu |
| `data/prices.csv` | Tüm günlerin birleşik tablosu |
| `data/run_log.csv` | Her çalışmanın gerçek zamanı ve OK/HATA durumu |

## Bilinen sınırlar (dürüstlük notu)

- Script canlı API'ye karşı **henüz test edilmedi** — sandbox'ta erişim engelliydi. Mock veriyle mantık doğrulandı.
  İlk gerçek çalıştırma 0 satır yazarsa muhtemelen alan adı farkıdır; `data/raw/` dosyası ne geldiğini gösterir.
- GitHub cron 10–30 dk kayabilir, nadiren atlayabilir. Atlanan gün `run_log`'da görünmez; bu yüzden
  haftada bir `run_log.csv`'de 7 satır olduğunu kontrol et. Eksik gün "ölçülmedi"dir, doldurulmaz.
- Şu an piyasa evreni her gün yeniden "en hacimli 100" olarak seçiliyor. Deney başlarken evren
  dondurulacak (Adım 2).
