for (const form of document.querySelectorAll("[data-label-editor]")) {
  const input = form.querySelector("input");
  const preview = form.querySelector("output");
  input.addEventListener("input", () => {
    const identifier = input.value.trim();
    preview.textContent = identifier ? `Busy (${identifier})` : "Busy";
  });
}
