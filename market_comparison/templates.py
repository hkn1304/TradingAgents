"""
Mode 4: Shopping list templates with exact product IDs.

Templates are stored as JSON in a local file. Each template entry records
the product ID (from marketfiyati) + display name + unit, so subsequent
comparisons fetch prices for that exact SKU across all chains.
"""
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime

STORE_PATH = Path(__file__).parent / "templates.json"


@dataclass
class TemplateItem:
    product_id: str       # e.g. "0002"
    name: str             # e.g. "Çokkolata Çikolatalı Bar 250 Gr"
    brand: str | None
    unit: str             # e.g. "250 GR"
    category: str


@dataclass
class Template:
    name: str
    items: list[TemplateItem] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class TemplateStore:
    def __init__(self, path: Path = STORE_PATH):
        self._path = path
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self):
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def list_templates(self) -> list[str]:
        return list(self._data.keys())

    def get(self, name: str) -> Template | None:
        raw = self._data.get(name)
        if not raw:
            return None
        items = [TemplateItem(**i) for i in raw.get("items", [])]
        return Template(
            name=raw["name"],
            items=items,
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
        )

    def save(self, template: Template):
        template.updated_at = datetime.utcnow().isoformat()
        self._data[template.name] = {
            "name": template.name,
            "items": [asdict(i) for i in template.items],
            "created_at": template.created_at,
            "updated_at": template.updated_at,
        }
        self._save()

    def delete(self, name: str) -> bool:
        if name in self._data:
            del self._data[name]
            self._save()
            return True
        return False

    def add_item(self, template_name: str, item: TemplateItem):
        tmpl = self.get(template_name) or Template(name=template_name)
        # Remove existing entry for same product_id if present
        tmpl.items = [i for i in tmpl.items if i.product_id != item.product_id]
        tmpl.items.append(item)
        self.save(tmpl)

    def remove_item(self, template_name: str, product_id: str):
        tmpl = self.get(template_name)
        if tmpl:
            tmpl.items = [i for i in tmpl.items if i.product_id != product_id]
            self.save(tmpl)
