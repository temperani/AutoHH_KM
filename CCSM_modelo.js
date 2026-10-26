const form = document.querySelector("#ccsm-form");
const printButton = document.querySelector("#print-button");

printButton.addEventListener("click", () => {
  if (!form.reportValidity()) return;
  window.print();
});

form.addEventListener("reset", () => {
  window.setTimeout(() => {
    form.querySelectorAll("[aria-invalid='true']").forEach((field) => {
      field.removeAttribute("aria-invalid");
    });
  }, 0);
});

form.addEventListener("change", (event) => {
  const field = event.target;
  if (field instanceof HTMLElement) {
    field.removeAttribute("aria-invalid");
  }
});
