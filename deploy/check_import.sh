#!/bin/bash
# Monitoring pobierania danych - alert mailem, gdy import stoi.
#
# Powstal po awarii z 11-12.09.2026: import aktow trafil na 13-megabajtowy PDF
# (M.P. 2021 poz. 414), parser zjadal cala pamiec procesu, kontener ginal i po
# restarcie bral ten sam akt od nowa. Przez dobe nie wszedl do bazy ani jeden
# nowy dokument - i nikt sie o tym nie dowiedzial, bo nic nie pilnowalo
# postepu. Ten skrypt pilnuje.
#
# Alert wysylany jest raz na problem (flaga), kasowany gdy wszystko wraca do normy,
# i PRZYPOMINANY co PRZYPOMNIENIE_H godzin, dopoki problem trwa. Przypomnienie
# dopisane po awarii z 12.09.2026: skrypt poprawnie wykryl restarty kontenera i
# wyslal alert o 15:20 UTC, po czym przez dwie godziny milczal, bo flaga blokuje
# powtorki. Awaria, ktorej nikt nie tknal przez pol dnia, ma sie odzywac.

FLAG=/home/deploy/orzeczenia/.import_alert_sent
PRZYPOMNIENIE_H=6
STATE=/home/deploy/orzeczenia/.import_restarts
MAIL=michal.kotlarz@gmail.com
PROBLEMY=""

# --- 1. Czy kontener nie restartuje sie w kolko -------------------------------
RESTARTY=$(sudo docker inspect orzecznik --format '{{.RestartCount}}' 2>/dev/null || echo 0)
POPRZEDNIO=$(head -1 "$STATE" 2>/dev/null)
[ -z "$POPRZEDNIO" ] && POPRZEDNIO="$RESTARTY"
PRZYROST=$((RESTARTY - POPRZEDNIO))
echo "$RESTARTY" > "$STATE"
if [ "$PRZYROST" -gt 3 ]; then
  PROBLEMY="${PROBLEMY}- kontener restartowal sie ${PRZYROST} razy od ostatniego sprawdzenia (lacznie ${RESTARTY})
"
fi

# --- 2. Czy import aktow w ogole odpowiada -----------------------------------
# Poprawna odpowiedz zawiera \"results\" albo komunikat o trwajacym przebiegu.
# Same znaczniki czasu bez tresci = kontener padal w trakcie zadania.
if [ -f /home/deploy/orzeczenia/akty_wstecz.log ]; then
  ODPOWIEDZI=$(tail -60 /home/deploy/orzeczenia/akty_wstecz.log | grep -c '{')
  if [ "$ODPOWIEDZI" -eq 0 ]; then
    PROBLEMY="${PROBLEMY}- import aktow nie zwrocil zadnej odpowiedzi w ostatnich 60 probach
"
  fi
fi

# --- 3. Czy cokolwiek przybywa ------------------------------------------------
WIEK_AKTU=$(sudo -u postgres psql orzecznik -At -c \
  "SELECT EXTRACT(EPOCH FROM (now() - MAX(first_seen_at)))/3600 FROM akty_prawne" 2>/dev/null \
  | cut -d. -f1)
if [ -n "$WIEK_AKTU" ] && [ "$WIEK_AKTU" -gt 24 ]; then
  PROBLEMY="${PROBLEMY}- od ${WIEK_AKTU} h nie przybyl zaden akt prawny
"
fi

WIEK_ORZ=$(sudo -u postgres psql orzecznik -At -c \
  "SELECT EXTRACT(EPOCH FROM (now() - MAX(first_seen_at)))/3600 FROM orzeczenia" 2>/dev/null \
  | cut -d. -f1)
# Sady nie publikuja w weekendy, wiec dla orzeczen prog jest luzniejszy.
if [ -n "$WIEK_ORZ" ] && [ "$WIEK_ORZ" -gt 72 ]; then
  PROBLEMY="${PROBLEMY}- od ${WIEK_ORZ} h nie przybylo zadne orzeczenie
"
fi

# --- 4. Czy serwis odpowiada --------------------------------------------------
if ! curl -s -m 15 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health | grep -q 200; then
  PROBLEMY="${PROBLEMY}- serwis nie odpowiada na /api/health
"
fi

# --- wyslanie -----------------------------------------------------------------
if [ -n "$PROBLEMY" ]; then
  # Wysylamy, gdy to nowy problem albo gdy poprzednia wiadomosc ma juz swoje lata.
  WYSLAC=0
  TEMAT="problem z pobieraniem danych"
  if [ ! -f "$FLAG" ]; then
    WYSLAC=1
  elif [ -n "$(find "$FLAG" -mmin +$((PRZYPOMNIENIE_H * 60)) 2>/dev/null)" ]; then
    WYSLAC=1
    TEMAT="problem TRWA od $(( ( $(date +%s) - $(stat -c %Y "$FLAG") ) / 3600 )) h"
  fi
  if [ "$WYSLAC" = 1 ]; then
    AKTY=$(sudo -u postgres psql orzecznik -At -c 'SELECT COUNT(*) FROM akty_prawne' 2>/dev/null)
    ORZ=$(sudo -u postgres psql orzecznik -At -c 'SELECT COUNT(*) FROM orzeczenia' 2>/dev/null)
    WOLNE=$(free -m | awk 'NR==2{print $7}')
    printf "Subject: Orzecznik - %s\n\nWykryte problemy:\n%b\nStan na teraz:\n- aktow prawnych w bazie: %s\n- orzeczen w bazie: %s\n- wolna pamiec: %s MB\n\nSerwer: 87.106.31.76 (portalorzeczen.pl)\nSprawdzone: %s\n\nCo warto zobaczyc najpierw:\n  sudo docker logs orzecznik --tail 50\n  tail -20 /home/deploy/orzeczenia/akty_wstecz.log\n  sudo dmesg -T | grep -i 'killed process' | tail\n" \
      "$TEMAT" "$PROBLEMY" "$AKTY" "$ORZ" "$WOLNE" "$(date -u)" | msmtp "$MAIL"
    # Odswiezenie znacznika czasu = odliczanie do nastepnego przypomnienia.
    touch "$FLAG"
  fi
else
  # Wszystko wrocilo do normy - kasujemy flage, zeby kolejny problem znow dal znac.
  if [ -f "$FLAG" ]; then
    AKTY=$(sudo -u postgres psql orzecznik -At -c 'SELECT COUNT(*) FROM akty_prawne' 2>/dev/null)
    printf "Subject: Orzecznik - pobieranie danych wrocilo do normy\n\nWczesniej zgloszony problem ustapil.\n\nAktow prawnych w bazie: %s\nSprawdzone: %s\n" \
      "$AKTY" "$(date -u)" | msmtp "$MAIL"
    rm -f "$FLAG"
  fi
fi
