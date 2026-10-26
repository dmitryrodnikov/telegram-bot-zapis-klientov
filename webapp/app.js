(function () {
  const root = document.getElementById("app");
  const telegram = window.Telegram && window.Telegram.WebApp;
  if (telegram) {
    telegram.ready();
    telegram.expand();
  }

  const state = { demo: false, service: null, day: null, slot: null, sending: false };

  function initData() {
    return telegram && telegram.initData ? telegram.initData : "";
  }

  function bannerText() {
    if (initData() || !state.demo) return "";
    return "Проверка в браузере: имя клиента не из Telegram. В боте профиль подставится сам.";
  }

  async function api(path, body) {
    const response = await fetch(path, body ? {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    } : undefined);
    const data = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      throw new Error(data.error || "Не получилось выполнить запрос");
    }
    return data;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function button(label, className, onClick) {
    const node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", function () {
      Promise.resolve(onClick()).catch(function (error) {
        showError(error.message || "Нет связи с ботом");
      });
    });
    return node;
  }

  function choice(title, meta, onClick) {
    const node = button("", "choice", onClick);
    node.appendChild(el("strong", "", title));
    if (meta) node.appendChild(el("small", "", meta));
    return node;
  }

  function view(title, hint, body, banner) {
    root.replaceChildren();
    if (banner) root.appendChild(el("p", "banner", banner));
    root.appendChild(el("h1", "", title));
    if (hint) root.appendChild(el("p", "hint", hint));
    root.appendChild(body);
  }

  function showError(message, back) {
    const body = el("div", "stack");
    body.appendChild(el("p", "error", message));
    body.appendChild(button("Назад", "primary", back || showServices));
    view("Не получилось", "", body, bannerText());
  }

  async function showServices() {
    const data = await api("/api/services");
    const body = el("div", "stack");
    data.services.forEach(function (service) {
      body.appendChild(choice(
        service.title,
        service.duration_min + " мин · " + service.price,
        function () { return showDays(service); },
      ));
    });
    body.appendChild(button("Мои записи", "ghost", showMine));
    view(
      "Запись на услуги",
      "Выберите услугу. На одно время записывается один человек.",
      body,
      bannerText(),
    );
  }

  async function showDays(service) {
    state.service = service;
    const data = await api("/api/days?service_id=" + encodeURIComponent(service.id));
    const body = el("div", "stack");
    body.appendChild(button("Назад", "back", showServices));
    if (!data.days.length) {
      body.appendChild(el("p", "hint", "В ближайшие дни свободного времени нет."));
    }
    data.days.forEach(function (day) {
      body.appendChild(choice(day.label, "Свободно окон: " + day.free, function () {
        return showSlots(service, day);
      }));
    });
    const hint = [service.duration_min + " мин · " + service.price, service.description]
      .filter(Boolean)
      .join("\n");
    view(service.title, hint, body, bannerText());
  }

  async function showSlots(service, day) {
    state.day = day;
    const data = await api(
      "/api/slots?service_id=" + encodeURIComponent(service.id) + "&day=" + encodeURIComponent(day.date),
    );
    const body = el("div", "stack");
    body.appendChild(button("Назад", "back", function () { return showDays(service); }));
    if (!data.slots.length) {
      body.appendChild(el("p", "hint", "На этот день свободного времени уже нет."));
    } else {
      const grid = el("div", "grid");
      data.slots.forEach(function (slot) {
        grid.appendChild(button(slot.label, "time", function () { return showConfirm(service, slot); }));
      });
      body.appendChild(grid);
    }
    view(service.title, day.label, body, bannerText());
  }

  function showConfirm(service, slot) {
    state.slot = slot;
    const body = el("div", "stack");
    body.appendChild(button("Назад", "back", function () { return showSlots(service, state.day); }));
    const card = el("div", "card");
    card.appendChild(el("strong", "", service.title));
    card.appendChild(el("p", "", slot.label));
    card.appendChild(el("p", "meta", service.price));
    body.appendChild(card);
    const submit = button("Подтвердить запись", "primary", function () { return submitBooking(submit); });
    body.appendChild(submit);
    view(
      "Проверьте запись",
      "После подтверждения это время исчезнет у других клиентов.",
      body,
      bannerText(),
    );
  }

  async function submitBooking(submit) {
    if (state.sending) return;
    state.sending = true;
    submit.disabled = true;
    try {
      const result = await api("/api/book", {
        service_id: state.service.id,
        starts_at: state.slot.starts_at,
        init_data: initData(),
      });
      const body = el("div", "stack");
      const card = el("div", "card");
      card.appendChild(el("strong", "", result.booking.title));
      card.appendChild(el("p", "", result.booking.when));
      card.appendChild(el("p", "meta", result.booking.price));
      body.appendChild(card);
      body.appendChild(button("Мои записи", "primary", showMine));
      body.appendChild(button("Новая запись", "ghost", showServices));
      view("Вы записаны", "Детали продублированы в чат с ботом.", body, bannerText());
    } catch (error) {
      showError(error.message, function () { return showSlots(state.service, state.day); });
    } finally {
      state.sending = false;
    }
  }

  async function showMine() {
    const data = await api("/api/my", { init_data: initData() });
    const body = el("div", "stack");
    body.appendChild(button("К услугам", "back", showServices));
    if (!data.bookings.length) {
      body.appendChild(el("p", "hint", "Активных записей нет."));
    }
    data.bookings.forEach(function (booking) {
      const card = el("div", "card");
      card.appendChild(el("strong", "", booking.title));
      card.appendChild(el("p", "", booking.when));
      card.appendChild(button("Отменить", "ghost", function () { return cancelBooking(booking.id); }));
      body.appendChild(card);
    });
    view("Мои записи", "", body, bannerText());
  }

  async function cancelBooking(id) {
    await api("/api/cancel", { id: id, init_data: initData() });
    await showMine();
  }

  async function boot() {
    try {
      const meta = await api("/api/meta");
      state.demo = Boolean(meta.demo);
      if (!initData() && !state.demo) {
        view(
          "Запись на услуги",
          "Откройте эту страницу кнопкой в боте Telegram.",
          el("div", "stack"),
          "",
        );
        return;
      }
      await showServices();
    } catch (error) {
      showError(error.message || "Нет связи с ботом");
    }
  }

  boot();
})();
