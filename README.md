# Haier Evo Air Conditioner integration
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs) [![stability-beta](https://img.shields.io/badge/stability-beta-33bbff.svg)](https://github.com/mkenney/software-guides/blob/master/STABILITY-BADGES.md#beta)

Неофициальная интеграция для работы с кондиционерами [Haier Evo](https://haieronline.ru/evo-iot/conditioners/) (RU, KZ, BY).

Бета-версия: может работать некорректно.

## Поддерживаемые устройства
Ниже приведен список устройств, работа с которыми была **проверена пользователями**. В действительности интеграция должна поддерживать больше устройств. Если ваше устройство поддерживается, но отсутствует в списке - сообщите в [Issues](https://github.com/and7ey/haier_evo/issues) об этом. Если устройство поддерживается приложением Evo, но не работает с интеграцией, то попробуйте в папке `devices` создать копию файла `AS20HPL2HRA.yaml` с именем модели вашего кондиционера. Сообщите результат, приложив [детальные логи](https://www.home-assistant.io/docs/configuration/troubleshooting/#enabling-debug-logging). Обратите внимание, что лог необходимо снимать в момент добавления кондиционера в интеграцию.

#### Серия Coral
<details>
<summary>AS20HPL1HRA, AS20HPL2HRA, AS25PHP2HRA, AS25PS1HRA, AS50HPL2HRA, HSU-07HPL203, HSU-09HPL203, HSU-12HPL203</summary>

- Поддерживается
  - Отображение текущей температуры
  - Включение/выключение
  - Режимы: авто, нагрев, охлаждение, осушение, вентилятор
  - Скорость работы вентилятора: 1, 2, 3, авто
  - Установка целевой температуры

- Не поддерживаются
  - Режимы: турбо, тихий
</details>

#### Серия Flexis
<details>
<summary>AS25S2SF2FA, AS35S2SF2FA, AS50S2SF2FA</summary>

- Поддерживается / не поддерживается
  - аналогично серии Coral
</details>

#### Серия Tundra
<details>
<summary>AS09TT5HRA</summary>

- Поддерживается / не поддерживается
  - аналогично серии Coral
</details>

#### Серия Casarte
<details>
<summary>CAS25CX1/R3-W</summary>

- Поддерживается / не поддерживается
  - аналогично серии Coral
</details>



## Совместимость
Haier предлагает разные мобильные приложения для разных рынков и стран. Некоторые устройства совместимы с более чем одним приложением. Эта интеграция поддерживает только устройства, которыми можно управлять через приложение [Evo](https://haieronline.ru/evo-iot/). Загрузите приложение Evo и проверьте совместимость с вашим устройством.
Приложения из этого (неполного) списка на данный момент запрошены:

| Приложение      | Рынок         | Совместимость                           | Альтернатива                                                                    |
|-----------------|---------------|-----------------------------------------|---------------------------------------------------------------------------------|
| Haier Evo       | Россия        | :heavy_check_mark:                      |                                                                                 |
| Haier hOn       | Европа        | :x:                                     | [Andre0512/hon](https://github.com/Andre0512/hon)                               |
