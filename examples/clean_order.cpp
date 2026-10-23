#include <stdexcept>
#include <string>
#include <vector>

int calculate_fee(int amount, int count) {
    if (count == 0) {
        throw std::invalid_argument("count must not be zero");
    }
    return amount / count;
}

std::string copy_label(const std::string& label) {
    return label;
}

std::vector<int> create_orders() {
    return std::vector<int>(10, 0);
}

bool is_paid(int state) {
    return state == 1;
}
