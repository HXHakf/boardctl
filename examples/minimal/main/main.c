/* blink —— boardctl 的最小验证工程
 *
 * 烧录：  python boardctl.py flash --port COMx
 * 验证：  python boardctl.py verify
 * 期望：  串口每 1 秒打印一次 ON / OFF，并且 evidence.jsonl 里多一行 source=host。
 */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"

#define BLINK_GPIO 2

void app_main(void)
{
    gpio_reset_pin(BLINK_GPIO);
    gpio_set_direction(BLINK_GPIO, GPIO_MODE_OUTPUT);

    bool on = false;
    for (;;) {
        on = !on;
        gpio_set_level(BLINK_GPIO, on);
        printf("%s\n", on ? "ON" : "OFF");
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
