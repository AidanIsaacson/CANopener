from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CanFrame:
    can_id: int
    data: bytes = b""
    extended: bool = True
    timestamp: float = 0.0
    tx: bool = False            # sent by this app
    error: str | None = None    # set for controller error frames (data is the raw error payload)

    @property
    def dlc(self) -> int:
        return len(self.data)

    def id_str(self) -> str:
        return f"{self.can_id:08X}" if self.extended else f"{self.can_id:03X}"
