#include <cstdio>
#include <stdexcept>

// 有意保留问题，用于 C++ 审查演示；请勿运行。
int calculate_fee(int amount) {
    return amount / 0;
}

void copy_label(const char* label) {
    char buffer[8];
    std::sprintf(buffer, "%s", label);
}

void release_orders() {
    int* orders = new int[10];
    delete orders;
}

bool is_paid(int state) {
    if (state = 1) {
        return true;
    }
    return false;
}

void load_order() {
    try {
        throw std::runtime_error("order unavailable");
    } catch (...) {
    }
}
