import flet as ft

def main (page: ft.Page):
    ft.Title = "FridgeIntel"
    page.add(
        ft.Text("Welcome!")
    )

if __name__ == "__main__":
    ft.run(main)
